'''
    @system: Ubuntu 16.04
    @pytorch version: 1.3
    @python version: 3.6
    @cuda version: 10.0
'''
from __future__ import print_function
import os
import sys
import shutil
import pickle
import time
import torch
import models
import torch.nn as nn
import numpy as np
from torch.autograd import Variable
from torch.utils.tensorboard import SummaryWriter
from evaluate import evaluate
from tensorboard_logger import configure, log_value
from options import args
from builtins import range
from utils import blockPrint, enablePrint, Vocabulary, decode_tokens
from data import get_train_loader

sys.path.append('./coco-caption/')
from pycocotools.coco import COCO

# initialize

configure(args.path_logs_tensorboard, flush_secs=10)
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
file_logs = open(args.path_logs, 'a')

## load vocabulary list
with open(args.vocab_pkl_path, 'rb') as f:
    vocab = pickle.load(f)
vocab_size = len(vocab)

## initialize model
model = models.build_model(args, vocab, DEVICE)
                                    
print('Total parameters:', sum(param.numel() for param in model.parameters()))
print('Total parameters:', sum(param.numel() for param in model.parameters()), file = file_logs)

print('model:', args.model)
print('model:', args.model, file = file_logs)
print('dataset:', args.dataset)
print('dataset:', args.dataset, file = file_logs)
print('beam size:', args.beam_size)
print('beam size:', args.beam_size, file = file_logs)
print('batch size:', args.batch_size)
print('batch size:', args.batch_size, file = file_logs)

## initialize loss function and optimizer
criterion = nn.CrossEntropyLoss()
optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate)

## initialize train_data_loader
train_loader = get_train_loader(args.train_caption_pkl_path, args.feature_h5_path, args.batch_size)
total_step = len(train_loader)

## initialize the ground-truth for reference
reference_json_path = '{0}.json'.format(args.test_reference_txt_path)
reference = COCO(reference_json_path)

# reload from last training

## reload model
if os.path.exists(args.model_pth_path) and args.use_checkpoint:
    model.load_state_dict(torch.load(args.model_pth_path))
model.to(DEVICE)

## reload optimizer
if os.path.exists(args.optimizer_pth_path) and args.use_checkpoint:
    optimizer.load_state_dict(torch.load(args.optimizer_pth_path))

print('Learning rate: %.5f' % args.learning_rate)
print('Batch size: %d' % args.batch_size)
print('Learning rate: %.5f' % args.learning_rate, file = file_logs)
print('Batch size: %d' % args.batch_size, file = file_logs)

# start training
best_meteor = 0
best_cider = 0
loss_count = 0
lr_decay_flag = 0
count_step = 0
v = None
t = None
e = None
saving_schedule = [int(x * total_step) for x in [0.25, 0.5, 0.75, 1.0]]

print('total: ', total_step)
print('saving_schedule: ', saving_schedule, file = file_logs)

start_time_train = time.time()

for epoch in range(args.num_epochs):
    start_time_epoch = time.time()
    if epoch % 10 ==0 and epoch > 0:
        args.learning_rate /= 10
    epsilon = max(0.6, args.ss_factor / (args.ss_factor + np.exp(epoch / args.ss_factor)))
    #epsilon = max(0.75, 1 - int(epoch / 5) * 0.05)
    
    print('epoch:%d\tepsilon:%.8f' % (epoch, epsilon))
    print('epoch:%d\tepsilon:%.8f' % (epoch, epsilon), file = file_logs)
    log_value('epsilon', epsilon, epoch)

    for i, (videos, captions, cap_lens, video_ids) in enumerate(train_loader, start=1):
        # transform data to tensor used for mini-batch
        #videos = torch.tensor(videos, requires_grad = True)
        #targets = torch.tensor(captions, requires_grad = True)
        videos = Variable(videos)
        targets = Variable(captions) # batch_size * max_words

        videos = videos.to(DEVICE)
        targets = targets.to(DEVICE)
        
        v, t = videos, targets
        e = epsilon

        optimizer.zero_grad()
        outputs = model(videos, targets, epsilon) # batch_size * num_time_steps_decoder(padding may be cut) * num_words_vocabulary
        tokens = outputs # batch_size * num_time_steps_decoder(padding may be cut) * num_words_vocabulary
        # 因为在一个epoch快要结束的时候，有可能采不到一个刚好的batch
        # 所以要重新计算一下batch size
        bsz = len(captions)
        # 把output压缩（剔除pad的部分）之后拉直
        outputs = torch.cat([outputs[j][:cap_lens[j]] for j in range(bsz)], 0) # may cut predicted words?
        #outputs = outputs.view(-1, vocab_size) # ?
        # 把target压缩（剔除pad的部分）之后拉直
        targets = torch.cat([targets[j][:cap_lens[j]] for j in range(bsz)], 0)
        #targets = targets.view(-1) # ?

        loss = criterion(outputs, targets)
        log_value('loss', loss.item(), epoch * total_step + i)
        loss_count += loss.item()
        loss.backward()
        optimizer.step()

        if i % 10 == 0 or bsz < args.batch_size:
            # calculate the average loss of consecutive 10 examples
            loss_count /= 10 if bsz == args.batch_size else i % 10
            
            print('Epoch [%d/%d], Step [%d/%d], Loss: %.4f, Perplexity: %5.4f' %
                  (epoch, args.num_epochs, i, total_step, loss_count, np.exp(loss_count)))
            print('Epoch [%d/%d], Step [%d/%d], Loss: %.4f, Perplexity: %5.4f' %
                  (epoch, args.num_epochs, i, total_step, loss_count, np.exp(loss_count)), 
                  file = file_logs)

            loss_count = 0

            # apply the model on the first example of current batch
            tokens = tokens.max(2)[1]
            #tokens = tokens[0].squeeze()
            tokens = tokens[0]
            we = decode_tokens(tokens, vocab)
            gt = decode_tokens(captions[0].squeeze(), vocab)

            print('[vid:%d]' % video_ids[0])
            print('WE: %s\nGT: %s' % (we, gt))
            print('[vid:%d]' % video_ids[0], file = file_logs)
            print('WE: %s\nGT: %s' % (we, gt), file = file_logs)

        if i in saving_schedule:
            torch.save(model.state_dict(), args.model_pth_path)
            torch.save(optimizer.state_dict(), args.optimizer_pth_path)

            # 计算一下在val集上的性能并记录下来
            blockPrint()
            start_time_eval = time.time()
            model.eval()
            metrics = evaluate(vocab, model, args.test_range, args.test_prediction_txt_path, reference)
            end_time_eval = time.time()
            enablePrint()

            print('evaluate time: %.3fs' % (end_time_eval-start_time_eval))
            print('evaluate time: %.3fs' % (end_time_eval-start_time_eval), file = file_logs)

            for k, v in metrics.items():
                log_value(k, v, epoch*len(saving_schedule) + count_step)
                
                print('%s: %.6f' % (k, v))
                print('%s: %.6f' % (k, v), file = file_logs)

                if k == 'METEOR' and v > best_meteor:
                    # 备份在val集上METEOR值最好的模型
                    shutil.copy2(args.model_pth_path, args.best_meteor_pth_path)
                    shutil.copy2(args.optimizer_pth_path, args.best_meteor_optimizer_pth_path)
                    best_meteor = v
                if k == 'CIDEr' and v > best_cider:
                    # 备份在val集上CIDEr值最好的模型
                    shutil.copy2(args.model_pth_path, args.best_cider_pth_path)
                    shutil.copy2(args.optimizer_pth_path, args.best_cider_optimizer_pth_path)
                    best_cider = v
                    lr_decay_flag = 0
                if k == 'CIDEr' and v < best_cider:
                    lr_decay_flag += 1

            # learning rate decay
            # if lr_decay_flag == 16:
            #     args.learning_rate /= 5
            #     lr_decay_flag = 0
            
            print('Step: %d, Learning rate: %.8f' % (epoch * len(saving_schedule) + count_step, args.learning_rate))
            print('Step: %d, Learning rate: %.8f' % (epoch * len(saving_schedule) + count_step, args.learning_rate), file = file_logs)

            optimizer = torch.optim.Adam(model.parameters(), lr=args.learning_rate)
            log_value('Learning rate', args.learning_rate, epoch*len(saving_schedule) + count_step)

            count_step += 1
            count_step %= 4
            model.train()


    end_time_epoch = time.time()

    print("**************One epoch time: %.3fs**************" % (end_time_epoch - start_time_epoch))
    print("****************past time: %.3fs****************" % (end_time_epoch - start_time_train))
    print("**************One epoch time: %.3fs**************" % (end_time_epoch - start_time_epoch), file = file_logs)
    print("****************past time: %.3fs****************" % (end_time_epoch - start_time_train), file = file_logs)

print('best_meteor:', best_meteor)
print('best_cider:', best_cider)
print('used hours: %.4fs' % ((time.time() - start_time_train) / 3600))
print('best_meteor:', best_meteor, file = file_logs)
print('best_cider:', best_cider, file = file_logs)

file_logs.close()

# with SummaryWriter(log_dir='./graph') as writer:
#     writer.add_graph(model, (v, t))