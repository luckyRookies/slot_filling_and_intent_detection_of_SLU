import argparse
import random
import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
import os, sys, time
import logging
import gc

install_path = os.path.abspath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(install_path)

import models.slot_tagger as slot_tagger
import models.slot_tagging_crf_sen_level as slot_tagger_with_crf_sen_level
import models.snt_classifier as snt_classifier

import utils.word_features as word_features
import utils.vocab_reader as vocab_reader
import utils.data_reader as data_reader
import utils.read_wordEmb as read_wordEmb
import utils.util as util
import utils.acc as acc

parser = argparse.ArgumentParser()
parser.add_argument('--task_st', required=True,
                    help='slot filling task: slot_tagger_sen_level | slot_tagger_with_crf_sen_level')
parser.add_argument('--task_sc', required=True,
                    help='intent detection task: none | 2tails | maxPooling | hiddenCNN | hiddenAttention')
parser.add_argument('--sc_type', default='single_cls_CE', help='single_cls_CE | multi_cls_BCE')
parser.add_argument('--st_weight', type=float, default=0.5,
                    help='loss weight for slot tagging task, ranging from 0 to 1.')

parser.add_argument('--dataset', required=True, help='atis-2 | snips')
parser.add_argument('--dataroot', required=True, help='path to dataset')
parser.add_argument('--save_model', default='model', help='save model to this file')
parser.add_argument('--noStdout', action='store_true', help='Only log to a file; no stdout')

parser.add_argument('--testing', action='store_true', help='Only test your model (default is training && testing)')
parser.add_argument('--read_model', required=False, help='Online test: read model from this file')
parser.add_argument('--out_path', required=False, help='Online test: out_path')

parser.add_argument('--read_input_sen2vec', required=True, help='read sentence embedding from sen2vec file')
parser.add_argument('--read_sen2idx', required=True, help='read sentence to index from sentences file')
parser.add_argument('--sen_max_len', type=int, default=76, help='max length of sentence in sentence bank from sentences file')

parser.add_argument('--emb_size', type=int, default=100, help='word embedding dimension')
parser.add_argument('--tag_emb_size', type=int, default=100, help='tag embedding dimension')
parser.add_argument('--hidden_size', type=int, default=100, help='hidden layer dimension')
parser.add_argument('--num_layers', type=int, default=1, help='number of hidden layers')
parser.add_argument('--bidirectional', action='store_true',
                    help='Whether to use bidirectional RNN (default is unidirectional)')

# parser.add_argument('--ngpu'  , type=int, default=1, help='number of GPUs to use')
parser.add_argument('--deviceId', type=int, default=-1, help='train model on ith gpu. -1:cpu, 0:auto_select')
parser.add_argument('--random_seed', type=int, default=999, help='set initial random seed')

parser.add_argument('--lr', type=float, default=0.01, help='learning rate')
parser.add_argument('--dropout', type=float, default=0., help='dropout rate at each non-recurrent layer')
parser.add_argument('--batchSize', type=int, default=64, help='input batch size')
parser.add_argument('--test_batchSize', type=int, default=0, help='input batch size in decoding')
parser.add_argument('--init_weight', type=float, default=0.2,
                    help='all weights will be set to [-init_weight, init_weight] during initialization')
parser.add_argument('--max_norm', type=float, default=5, help="threshold of gradient clipping (2-norm)")
parser.add_argument('--max_epoch', type=int, default=50, help='max number of epochs to train for')
parser.add_argument('--experiment', default='exp', help='Where to store samples and models')
parser.add_argument('--optim', default='sgd', help='choose an optimizer')

opt = parser.parse_args()

assert opt.testing == bool(opt.out_path) == bool(opt.read_model)

if opt.test_batchSize == 0:
    opt.test_batchSize = opt.batchSize

assert opt.task_st in {'slot_tagger_sen_level', 'slot_tagger_with_crf_sen_level'}
assert opt.task_sc in {'none', '2tails', 'maxPooling', 'hiddenCNN', 'hiddenAttention'}
assert opt.sc_type in {'single_cls_CE', 'multi_cls_BCE'}
if opt.sc_type == 'multi_cls_BCE':
    opt.multiClass = True
else:
    opt.multiClass = False

if opt.task_st == 'slot_tagger_with_crf_sen_level':
    opt.crf = True
else:
    opt.crf = False

assert 0 < opt.st_weight <= 1
if opt.st_weight == 1 or opt.task_sc == 'none':
    opt.task_sc = None

# construct experiment log path
if not opt.testing:
    if opt.task_sc:
        opt.task = opt.task_st + '__and__' + opt.task_sc
    else:
        opt.task = opt.task_st
    exp_path = util.hyperparam_string(opt)
    exp_path = os.path.join(opt.experiment, exp_path)
    exp_path += '__tes_%s' % (opt.tag_emb_size)
    if opt.task_sc:
        exp_path += '__alpha_%s' % (opt.st_weight)
    if opt.read_input_sen2vec:
        exp_path += '__preSenEmb_in'

else:
    exp_path = opt.out_path
if not os.path.exists(exp_path):
    os.makedirs(exp_path)

# construct fileHandler(and consoleHandler) to logger
logFormatter = logging.Formatter('%(message)s')  # ('%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('mylogger')
logger.setLevel(logging.DEBUG)
if opt.testing:
    fileHandler = logging.FileHandler('%s/log_test.txt' % (exp_path), mode='w')
else:
    fileHandler = logging.FileHandler('%s/log_train.txt' % (exp_path), mode='w')
fileHandler.setFormatter(logFormatter)
logger.addHandler(fileHandler)
if not opt.noStdout:
    consoleHandler = logging.StreamHandler(sys.stdout)
    consoleHandler.setFormatter(logFormatter)
    logger.addHandler(consoleHandler)
logger.info(opt)
logger.info("Experiment path: %s" % (exp_path))
logger.info(time.asctime(time.localtime(time.time())))

if opt.deviceId >= 0:
    import utils.gpu_selection as gpu_selection

    if opt.deviceId > 0:
        opt.deviceId, gpu_name, valid_gpus = gpu_selection.auto_select_gpu(assigned_gpu_id=opt.deviceId - 1)
    elif opt.deviceId == 0:
        opt.deviceId, gpu_name, valid_gpus = gpu_selection.auto_select_gpu()
    logger.info("Valid GPU list: %s ; GPU %d (%s) is auto selected." % (valid_gpus, opt.deviceId, gpu_name))
    torch.cuda.set_device(opt.deviceId)
    opt.device = torch.device(
        "cuda")  # is equivalent to torch.device('cuda:X') where X is the result of torch.cuda.current_device()
else:
    logger.info("CPU is used.")
    opt.device = torch.device("cpu")

random.seed(opt.random_seed)
torch.manual_seed(opt.random_seed)
if torch.cuda.is_available():
    if opt.device.type != 'cuda':
        logger.info("WARNING: You have a CUDA device, so you should probably run with --deviceId [1|2|3]")
    else:
        torch.cuda.manual_seed(opt.random_seed)
np.random.seed(opt.random_seed)

dataroot = opt.dataroot

tag_vocab_dir = dataroot + '/vocab.slot'
class_vocab_dir = dataroot + '/vocab.intent'
train_data_dir = dataroot + '/train'
valid_data_dir = dataroot + '/valid'
test_data_dir = dataroot + '/test'

if not opt.testing:
    # training mode config
    # load word & slot & intent vocab
    vocab_config = {'mini_word_freq': 2, 'bos_eos': False, 'lowercase': False}
    tag_to_idx, idx_to_tag = vocab_reader.read_vocab_file(tag_vocab_dir, bos_eos=False)
    class_to_idx, idx_to_class = vocab_reader.read_vocab_file(class_vocab_dir, bos_eos=False)
    word_to_idx, idx_to_word = vocab_reader.read_vocab_from_data_file(train_data_dir, vocab_config=vocab_config)
    logger.info("Vocab size: %s %s %s" % (len(word_to_idx), len(tag_to_idx), len(class_to_idx)))

    # load sentence embedding
    ext_sen_emb = read_wordEmb.read_sen2vec_inText(opt.read_input_sen2vec, opt.device)
    ext_sen_size = len(ext_sen_emb)
    sen2idx = data_reader.read_sen_bank(opt.read_sen2idx)
    logger.info('Sentence size: %s, sentence_embedding size: %s, sen2idx size: %s' % (
    ext_sen_size, len(ext_sen_emb), len(sen2idx)))

    # word features
    train_feats, train_tags, train_class = data_reader.read_seqtag_data_with_class(train_data_dir, word_to_idx,
                                                                                   tag_to_idx, class_to_idx,
                                                                                   multiClass=opt.multiClass,
                                                                                   lowercase=False)
    valid_feats, valid_tags, valid_class = data_reader.read_seqtag_data_with_class(valid_data_dir, word_to_idx,
                                                                                   tag_to_idx, class_to_idx,
                                                                                   multiClass=opt.multiClass,
                                                                                   keep_order=opt.testing,
                                                                                   lowercase=False)
    test_feats, test_tags, test_class = data_reader.read_seqtag_data_with_class(test_data_dir, word_to_idx, tag_to_idx,
                                                                                class_to_idx, multiClass=opt.multiClass,
                                                                                keep_order=opt.testing,
                                                                                lowercase=False)

    # sentence features
    train_sen_feats = data_reader.read_sen_feats(train_data_dir, sen2idx)
    valid_sen_feats = data_reader.read_sen_feats(valid_data_dir, sen2idx)
    test_sen_feats = data_reader.read_sen_feats(test_data_dir, sen2idx)

# construct slot tagging model
if opt.task_st == 'slot_tagger_with_crf_sen_level':
    model_tag = slot_tagger_with_crf_sen_level.LSTMTagger_CRF_sen_level(opt.emb_size, opt.sen_max_len, ext_sen_size,
                                                                        opt.hidden_size, len(tag_to_idx),
                                                                        bidirectional=opt.bidirectional,
                                                                        num_layers=opt.num_layers,
                                                                        dropout=opt.dropout, device=opt.device)


# construct intent classification mode;
if opt.task_sc == '2tails':
    model_class = snt_classifier.sntClassifier_2tails(opt.hidden_size, len(class_to_idx),
                                                      bidirectional=opt.bidirectional, num_layers=opt.num_layers,
                                                      dropout=opt.dropout, device=opt.device,
                                                      multi_class=opt.multiClass)
    encoder_info_filter = lambda info: info[0]
elif opt.task_sc == 'maxPooling':
    model_class = snt_classifier.sntClassifier_hiddenPooling(opt.hidden_size, len(class_to_idx),
                                                             bidirectional=opt.bidirectional,
                                                             num_layers=opt.num_layers,
                                                             dropout=opt.dropout, device=opt.device,
                                                             multi_class=opt.multiClass, pooling='max')
    encoder_info_filter = lambda info: (info[1], info[2])
elif opt.task_sc == 'hiddenCNN':
    model_class = snt_classifier.sntClassifier_hiddenCNN(opt.hidden_size, len(class_to_idx),
                                                         bidirectional=opt.bidirectional, num_layers=opt.num_layers,
                                                         dropout=opt.dropout, device=opt.device,
                                                         multi_class=opt.multiClass)
    encoder_info_filter = lambda info: (info[1], info[2])
elif opt.task_sc == 'hiddenAttention':
    model_class = snt_classifier.sntClassifier_hiddenAttention(opt.hidden_size, len(class_to_idx),
                                                               bidirectional=opt.bidirectional,
                                                               num_layers=opt.num_layers, dropout=opt.dropout,
                                                               device=opt.device, multi_class=opt.multiClass)
    encoder_info_filter = lambda info: info
else:
    pass

model_tag = model_tag.to(opt.device)
if opt.task_sc:
    model_class = model_class.to(opt.device)

# training mode
if not opt.testing:
    # custom init (needed maybe) ...
    model_tag.init_weights(opt.init_weight)
    if opt.task_sc:
        model_class.init_weights(opt.init_weight)

    # pretrained_sen_embedding by HG
    if opt.read_input_sen2vec:
        for i in range(ext_sen_size):
            model_tag.sen_embeddings.weight.data[i] = ext_sen_emb[i]

# loss function
weight_mask = torch.ones(len(tag_to_idx), device=opt.device)
weight_mask[tag_to_idx['<pad>']] = 0
tag_loss_function = nn.NLLLoss(weight=weight_mask, size_average=False)
if opt.task_sc:
    if opt.multiClass:
        class_loss_function = nn.BCELoss(size_average=False)
    else:
        class_loss_function = nn.NLLLoss(size_average=False)

# optimizer
params = []
params += list(model_tag.parameters())
if opt.task_sc:
    params += list(model_class.parameters())
params = list(filter(lambda p: p.requires_grad, params))  # must be list, otherwise clip_grad_norm_ will be invalid
if opt.optim.lower() == 'sgd':
    optimizer = optim.SGD(params, lr=opt.lr)
elif opt.optim.lower() == 'adam':
    optimizer = optim.Adam(params, lr=opt.lr, betas=(0.9, 0.999), eps=1e-8, weight_decay=0)  # (beta1, beta2)
elif opt.optim.lower() == 'adadelta':
    optimizer = optim.Adadelta(params, rho=0.95, lr=1.0)
elif opt.optim.lower() == 'rmsprop':
    optimizer = optim.RMSprop(params, lr=opt.lr)

def decode(sen_feats, data_feats, data_tags, data_class, output_path):
    data_index = np.arange(len(data_feats))
    losses = []
    TP, FP, FN, TN = 0.0, 0.0, 0.0, 0.0
    TP2, FP2, FN2, TN2 = 0.0, 0.0, 0.0, 0.0
    with open(output_path, 'w') as f:
        for j in range(0, len(data_index), opt.test_batchSize):
            if opt.testing:
                inputs, tags, raw_tags, classes, raw_classes, lens, line_nums = data_reader.get_minibatch_with_class(
                    data_feats, data_tags, data_class, word_to_idx, tag_to_idx, class_to_idx, data_index, j,
                    opt.test_batchSize, add_start_end=False, multiClass=opt.multiClass, keep_order=opt.testing,
                    enc_dec_focus=False, device=opt.device)
                input_sens = data_reader.get_sen_minibatch(sen_feats, train_data_index, j, opt.batchSize, device=opt.device)
            else:
                inputs, tags, raw_tags, classes, raw_classes, lens = data_reader.get_minibatch_with_class(data_feats,
                                                                                                          data_tags,
                                                                                                          data_class,
                                                                                                          word_to_idx,
                                                                                                          tag_to_idx,
                                                                                                          class_to_idx,
                                                                                                          data_index, j,
                                                                                                          opt.test_batchSize,
                                                                                                          add_start_end=False,
                                                                                                          multiClass=opt.multiClass,
                                                                                                          keep_order=opt.testing,
                                                                                                          enc_dec_focus=False,
                                                                                                          device=opt.device)
                input_sens = data_reader.get_sen_minibatch(sen_feats, data_index, j, opt.batchSize, device=opt.device)

            if opt.crf:
                max_len = max(lens)
                masks = [([1] * l) + ([0] * (max_len - l)) for l in lens]
                masks = torch.tensor(masks, dtype=torch.uint8, device=opt.device)

                crf_feats, encoder_info = model_tag._get_lstm_features(input_sens, lens, with_snt_classifier=True)
                tag_path_scores, tag_path = model_tag.forward(crf_feats, masks)
                tag_loss = model_tag.neg_log_likelihood(crf_feats, masks, tags)
                top_pred_slots = tag_path.data.cpu().numpy()
            else:
                tag_scores, encoder_info = model_tag(inputs, lens, with_snt_classifier=True)
                tag_loss = tag_loss_function(tag_scores.contiguous().view(-1, len(tag_to_idx)), tags.view(-1))
                top_pred_slots = tag_scores.data.cpu().numpy().argmax(axis=-1)
                # tags = tags.data.cpu().numpy()
            if opt.task_sc:
                class_scores = model_class(encoder_info_filter(encoder_info))
                class_loss = class_loss_function(class_scores, classes)
                if opt.multiClass:
                    snt_probs = class_scores.data.cpu().numpy()
                else:
                    snt_probs = class_scores.data.cpu().numpy().argmax(axis=-1)
                losses.append([tag_loss.item() / sum(lens), class_loss.item() / len(lens)])
            else:
                losses.append([tag_loss.item() / sum(lens), 0])

            inputs = inputs.data.cpu().numpy()
            # classes = classes.data.cpu().numpy()
            for idx, pred_line in enumerate(top_pred_slots):
                length = lens[idx]
                pred_seq = [idx_to_tag[tag] for tag in pred_line][:length]
                lab_seq = [idx_to_tag[tag] if type(tag) == int else tag for tag in raw_tags[idx]]
                pred_chunks = acc.get_chunks(['O'] + pred_seq + ['O'])
                label_chunks = acc.get_chunks(['O'] + lab_seq + ['O'])
                for pred_chunk in pred_chunks:
                    if pred_chunk in label_chunks:
                        TP += 1
                    else:
                        FP += 1
                for label_chunk in label_chunks:
                    if label_chunk not in pred_chunks:
                        FN += 1

                input_line = [idx_to_word[word] for word in inputs[idx]][:length]
                word_tag_line = [input_line[_idx] + ':' + lab_seq[_idx] + ':' + pred_seq[_idx] for _idx in
                                 range(len(input_line))]

                if opt.task_sc:
                    if opt.multiClass:
                        pred_classes = [idx_to_class[i] for i, p in enumerate(snt_probs[idx]) if p > 0.5]
                        gold_classes = [idx_to_class[i] for i in raw_classes[idx]]
                        for pred_class in pred_classes:
                            if pred_class in gold_classes:
                                TP2 += 1
                            else:
                                FP2 += 1
                        for gold_class in gold_classes:
                            if gold_class not in pred_classes:
                                FN2 += 1
                        gold_class_str = ';'.join(gold_classes)
                        pred_class_str = ';'.join(pred_classes)
                    else:
                        pred_class = idx_to_class[snt_probs[idx]]
                        if type(raw_classes[idx]) == int:
                            gold_classes = {idx_to_class[raw_classes[idx]]}
                        else:
                            gold_classes = set(raw_classes[idx])
                        if pred_class in gold_classes:
                            TP2 += 1
                        else:
                            FP2 += 1
                            FN2 += 1
                        gold_class_str = ';'.join(list(gold_classes))
                        pred_class_str = pred_class
                else:
                    gold_class_str = ''
                    pred_class_str = ''

                if opt.testing:
                    f.write(str(line_nums[idx]) + ' : ' + ' '.join(
                        word_tag_line) + ' <=> ' + gold_class_str + ' <=> ' + pred_class_str + '\n')
                else:
                    f.write(' '.join(word_tag_line) + ' <=> ' + gold_class_str + ' <=> ' + pred_class_str + '\n')

    if TP == 0:
        p, r, f = 0, 0, 0
    else:
        p, r, f = 100 * TP / (TP + FP), 100 * TP / (TP + FN), 100 * 2 * TP / (2 * TP + FN + FP)

    if TP2 == 0:
        cp, cr, cf = 0, 0, 0
    else:
        cp, cr, cf = 100 * TP2 / (TP2 + FP2), 100 * TP2 / (TP2 + FN2), 100 * 2 * TP2 / (2 * TP2 + FN2 + FP2)

    mean_losses = np.mean(losses, axis=0)
    return mean_losses, p, r, f, cp, cr, cf  # 0 if 2*TP2+FN2+FP2 == 0 else 100*2*TP2/(2*TP2+FN2+FP2)

# training mode
if not opt.testing:
    logger.info("Training starts at %s" % (time.asctime(time.localtime(time.time()))))
    train_data_index = np.arange(len(train_sen_feats))
    best_f1, best_result = -1, {}

    for i in range(opt.max_epoch):
        start_time = time.time()
        losses = []
        # training data shuffle
        np.random.shuffle(train_data_index)
        model_tag.train()
        if opt.task_sc:
            model_class.train()

        nsentences = len(train_data_index)
        piece_sentences = opt.batchSize if int(nsentences * 0.1 / opt.batchSize) == 0 else int(
            nsentences * 0.1 / opt.batchSize) * opt.batchSize
        for j in range(0, nsentences, opt.batchSize):
            inputs, tags, raw_tags, classes, raw_classes, lens = data_reader.get_minibatch_with_class(
                train_feats['data'], train_tags['data'], train_class['data'], word_to_idx, tag_to_idx, class_to_idx,
                train_data_index, j, opt.batchSize, add_start_end=False, multiClass=opt.multiClass,
                enc_dec_focus=False, device=opt.device)
            input_sens = data_reader.get_sen_minibatch(train_sen_feats, train_data_index, j, opt.batchSize, device=opt.device)

            if opt.crf:
                max_len = max(lens)
                masks = [([1] * l) + ([0] * (max_len - l)) for l in lens]
                masks = torch.tensor(masks, dtype=torch.uint8, device=opt.device)

                crf_feats, encoder_info = model_tag._get_lstm_features(input_sens, lens, with_snt_classifier=True)
                tag_loss = model_tag.neg_log_likelihood(crf_feats, masks, tags)
            else:
                tag_scores, encoder_info = model_tag(inputs, lens, with_snt_classifier=True)
                tag_loss = tag_loss_function(tag_scores.contiguous().view(-1, len(tag_to_idx)), tags.view(-1))

            if opt.task_sc:
                class_scores = model_class(encoder_info_filter(encoder_info))
                class_loss = class_loss_function(class_scores, classes)
                losses.append([tag_loss.item() / sum(lens), class_loss.item() / len(lens)])
                total_loss = opt.st_weight * tag_loss + (1 - opt.st_weight) * class_loss
            else:
                losses.append([tag_loss.item() / sum(lens), 0])
                total_loss = tag_loss
            total_loss.backward()

            # Clips gradient norm of an iterable of parameters.
            if opt.max_norm > 0:
                torch.nn.utils.clip_grad_norm_(params, opt.max_norm)

            optimizer.step()

            if j % piece_sentences == 0:
                print('[learning] epoch %i >> %2.2f%%' % (i, (j + opt.batchSize) * 100. / nsentences),
                      'completed in %.2f (sec) <<\r' % (time.time() - start_time), end='')
                sys.stdout.flush()

        mean_loss = np.mean(losses, axis=0)
        logger.info('Training:\tEpoch : %d\tTime : %.4fs\tLoss of tag : %.2f\tLoss of class : %.2f ' % (
            i, time.time() - start_time, mean_loss[0], mean_loss[1]))
        gc.collect()

        model_tag.eval()
        if opt.task_sc:
            model_class.eval()
        # Evaluation
        start_time = time.time()

        loss_val, p_val, r_val, f_val, cp_val, cr_val, cf_val = decode(valid_sen_feats, valid_feats['data'],
                                                                       valid_tags['data'],
                                                                       valid_class['data'],
                                                                       os.path.join(exp_path,
                                                                                    'valid.iter' + str(i)))
        logger.info(
            'Validation:\tEpoch : %d\tTime : %.4fs\tLoss : (%.2f, %.2f)\tP: %.2f, R: %.2f, Fscore : %.2f\tcls-P: %.2f, cls-R: %.2f, cls-F1 : %.2f ' % (
                i, time.time() - start_time, loss_val[0], loss_val[1], p_val, r_val, f_val, cp_val, cr_val, cf_val))
        start_time = time.time()
        loss_te, p_te, r_te, f_te, cp_te, cr_te, cf_te = decode(test_sen_feats, test_feats['data'],
                                                                test_tags['data'],
                                                                test_class['data'],
                                                                os.path.join(exp_path, 'test.iter' + str(i)))
        logger.info(
            'Evaluation:\tEpoch : %d\tTime : %.4fs\tLoss : (%.2f, %.2f)\tP: %.2f, R: %.2f, Fscore : %.2f\tcls-P: %.2f, cls-R: %.2f, cls-F1 : %.2f ' % (
                i, time.time() - start_time, loss_te[0], loss_te[1], p_te, r_te, f_te, cp_te, cr_te, cf_te))

        if opt.task_sc:
            val_f1_score = (opt.st_weight * f_val + (1 - opt.st_weight) * cf_val)
        else:
            val_f1_score = f_val
        if best_f1 < val_f1_score:
            model_tag.save_model(os.path.join(exp_path, opt.save_model + '.tag'))
            if opt.task_sc:
                model_class.save_model(os.path.join(exp_path, opt.save_model + '.class'))
            best_f1 = val_f1_score
            logger.info(
                'NEW BEST:\tEpoch : %d\tbest valid P: %.2f, R: %.2f, F1 : %.2f, cls-P: %.2f, cls-R: %.2f, cls-F1 : %.2f;\ttest P: %.2f, R: %.2f, F1 : %.2f, cls-P: %.2f, cls-R: %.2f, cls-F1 : %.2f' % (
                    i, p_val, r_val, f_val, cp_val, cr_val, cf_val, p_te, r_te, f_te, cp_te, cr_te, cf_te))
            best_result['iter'] = i
            best_result['vp'], best_result['vr'], best_result['vf1'], best_result['vcp'], best_result['vcr'], \
            best_result['vcf1'], best_result['vce'] = p_val, r_val, f_val, cp_val, cr_val, cf_val, loss_val
            best_result['tp'], best_result['tr'], best_result['tf1'], best_result['tcp'], best_result['tcr'], \
            best_result['tcf1'], best_result['tce'] = p_te, r_te, f_te, cp_te, cr_te, cf_te, loss_te

    # logger.info(
    #     'BEST RESULT: \tEpoch : %d\tbest valid P: %.2f, R: %.2f, F1 : %.2f; cls-P: %.2f, cls-R: %.2f, cls-F1 : %.2f)' % (
    #     best_result['iter'], best_result['vp'], best_result['vr'], best_result['vf1'], best_result['vcp'],
    #     best_result['vcr'], best_result['vcf1']))

    logger.info(
        'BEST RESULT: \tEpoch : %d\tbest valid P: %.2f, R: %.2f, F1 : %.2f; cls-P: %.2f, cls-R: %.2f, cls-F1 : %.2f)\tbest test P: %.2f, R: %.2f, F1 : %.2f; cls-P: %.2f, cls-R: %.2f, cls-F1 : %.2f) ' % (
            best_result['iter'], best_result['vp'], best_result['vr'], best_result['vf1'], best_result['vcp'],
            best_result['vcr'], best_result['vcf1'], best_result['tp'], best_result['tr'], best_result['tf1'],
            best_result['tcp'], best_result['tcr'], best_result['tcf1']))

























