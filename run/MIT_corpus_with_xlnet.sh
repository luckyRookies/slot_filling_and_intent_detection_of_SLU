#!/bin/bash

source ./path.sh

task_slot_filling=$1 #slot_tagger, slot_tagger_with_crf, slot_tagger_with_focus
task_intent_detection=none # none, hiddenAttention, hiddenCNN, maxPooling, 2tails
balance_weight=1

pretrained_model_type=xlnet
pretrained_model_name=xlnet-base-cased #xlnet-large-cased

dataroot=data/MIT_corpus/$2 #movie_eng, movie_trivia10k13, restaurant
dataset=mit_$2

lstm_hidden_size=200 # 100, 200
lstm_layers=1
slot_tag_embedding_size=100  ## for slot_tagger_with_focus
batch_size=32 # 16, 32
test_batchSize=16

optimizer=bertadam # bertadam, adamw
learning_rate=5e-5 # 1e-5, 5e-5, 1e-4, 1e-3
max_norm_of_gradient_clip=1 # working for adamw
dropout_rate=0.1 # 0.1, 0.5

max_epoch=20

device=0
# device=0 means auto-choosing a GPU
# Set deviceId=-1 if you are going to use cpu for training.
experiment_output_path=exp

python scripts/slot_tagging_and_intent_detection_with_transformer.py --task_st $task_slot_filling --task_sc $task_intent_detection --dataset $dataset --dataroot $dataroot --bidirectional --lr $learning_rate --dropout $dropout_rate --batchSize $batch_size --optim $optimizer --max_norm $max_norm_of_gradient_clip --experiment $experiment_output_path --deviceId $device --max_epoch $max_epoch --hidden_size $lstm_hidden_size --num_layers ${lstm_layers} --tag_emb_size $slot_tag_embedding_size --st_weight ${balance_weight} --pretrained_model_type ${pretrained_model_type} --pretrained_model_name ${pretrained_model_name}
