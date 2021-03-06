# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""BERT finetuning runner."""

import argparse
import csv
import logging
import os
import random
import sys
from io import open

import numpy as np
import torch
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange

from pytorch_pretrained_bert.file_utils import PYTORCH_PRETRAINED_BERT_CACHE
from pytorch_pretrained_bert.modeling import BertForMultipleChoice, BertForSequenceClassification, BertConfig
from pytorch_pretrained_bert.optimization import BertAdam, warmup_linear
from pytorch_pretrained_bert.tokenization import BertTokenizer

logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt = '%m/%d/%Y %H:%M:%S',
                    level = logging.INFO)
logger = logging.getLogger(__name__)


class SwagExample(object):
    """A single training/test example for the SWAG dataset."""
    def __init__(self,
                 swag_id,
                 context_sentence,
                 start_ending,
                 ending_0,
                 ending_1,
                 ending_2,
                 ending_3,
                 label = None,
                 protected_attr = None):
        self.swag_id = swag_id
        self.context_sentence = context_sentence
        self.start_ending = start_ending
        self.endings = [
            ending_0,
            ending_1,
            ending_2,
            ending_3,
        ]
        self.label = label
        self.protected_attr = protected_attr

    def __str__(self):
        return self.__repr__()

    def __repr__(self):
        l = [
            "swag_id: {}".format(self.swag_id),
            "context_sentence: {}".format(self.context_sentence),
            "start_ending: {}".format(self.start_ending),
            "ending_0: {}".format(self.endings[0]),
            "ending_1: {}".format(self.endings[1]),
            "ending_2: {}".format(self.endings[2]),
            "ending_3: {}".format(self.endings[3]),
        ]

        if self.label is not None:
            l.append("label: {}".format(self.label))
        if self.protected_attr is not None:
            l.append("protected_attr: {}".format(self.protected_attr))

        return ", ".join(l)


class InputFeatures(object):
    def __init__(self,
                 example_id,
                 choices_features,
                 label,
                 protected_attr,
                 endings

    ):
        self.example_id = example_id
        self.choices_features = [
            {
                'input_ids': input_ids,
                'input_mask': input_mask,
                'segment_ids': segment_ids,
                'vp_input_ids': vp_input_ids,
                'vp_input_mask': vp_input_mask
            }
            for _, input_ids, input_mask, segment_ids, _, vp_input_ids, vp_input_mask in choices_features
        ]
        self.label = label
        self.protected_attr = protected_attr
        self.endings = endings


def read_swag_examples(input_file, is_training):
    with open(input_file, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        lines = []
        for line in reader:
            if sys.version_info[0] == 2:
                line = list(unicode(cell, 'utf-8') for cell in line)
            lines.append(line)

    if is_training and 'label' not in lines[0]:
        raise ValueError(
            "For training, the input file must contain a label column."
        )

    examples = [
        SwagExample(
            swag_id = line[2],
            context_sentence = line[4],
            start_ending = line[5], # in the swag dataset, the
                                         # common beginning of each
                                         # choice is stored in "sent2".
            ending_0 = line[7],
            ending_1 = line[8],
            ending_2 = line[9],
            ending_3 = line[10],
            label = int(line[11]) if is_training else None,
            protected_attr = int(line[12]) if is_training else None
        ) for line in lines[1:] # we skip the line with the column names
    ]

    return examples

def convert_examples_to_features(examples, tokenizer, max_seq_length,
                                 is_training):
    """Loads a data file into a list of `InputBatch`s."""

    # Swag is a multiple choice task. To perform this task using Bert,
    # we will use the formatting proposed in "Improving Language
    # Understanding by Generative Pre-Training" and suggested by
    # @jacobdevlin-google in this issue
    # https://github.com/google-research/bert/issues/38.
    #
    # Each choice will correspond to a sample on which we run the
    # inference. For a given Swag example, we will create the 4
    # following inputs:
    # - [CLS] context [SEP] choice_1 [SEP]
    # - [CLS] context [SEP] choice_2 [SEP]
    # - [CLS] context [SEP] choice_3 [SEP]
    # - [CLS] context [SEP] choice_4 [SEP]
    # The model will output a single value for each input. To get the
    # final decision of the model, we will run a softmax over these 4
    # outputs.
    features = []
    for example_index, example in enumerate(examples):
        context_tokens = tokenizer.tokenize(example.context_sentence)
        start_ending_tokens = tokenizer.tokenize(example.start_ending)

        choices_features = []
        for ending_index, ending in enumerate(example.endings):
            # We create a copy of the context tokens in order to be
            # able to shrink it according to ending_tokens
            context_tokens_choice = context_tokens[:]
            vp_token_ = tokenizer.tokenize(ending)
            ending_tokens = start_ending_tokens + vp_token_
            # Modifies `context_tokens_choice` and `ending_tokens` in
            # place so that the total length is less than the
            # specified length.  Account for [CLS], [SEP], [SEP] with
            # "- 3"
            _truncate_seq_pair(context_tokens_choice, ending_tokens, max_seq_length - 3)

            vp_tokens = ["[CLS]"] + vp_token_ + ["[SEP]"]
            
            tokens = ["[CLS]"] + context_tokens_choice + ["[SEP]"] + ending_tokens + ["[SEP]"]
            segment_ids = [0] * (len(context_tokens_choice) + 2) + [1] * (len(ending_tokens) + 1)
            
            input_ids = tokenizer.convert_tokens_to_ids(tokens)
            input_mask = [1] * len(input_ids)
            
            vp_input_ids = tokenizer.convert_tokens_to_ids(vp_tokens)
            vp_input_mask = [1] * len(vp_input_ids)

            # Zero-pad up to the sequence length.
            padding = [0] * (max_seq_length - len(input_ids))
            input_ids += padding
            input_mask += padding
            segment_ids += padding
            
            padding = [0] * (int(max_seq_length / 2) - len(vp_input_ids))
            vp_input_ids += padding
            vp_input_mask += padding
            
            assert len(input_ids) == max_seq_length
            assert len(input_mask) == max_seq_length
            assert len(segment_ids) == max_seq_length
            assert len(vp_input_ids) == int(max_seq_length / 2)
            assert len(vp_input_mask) == int(max_seq_length / 2)
            choices_features.append((tokens, input_ids, input_mask, segment_ids, vp_tokens, vp_input_ids, vp_input_mask))

        label = example.label
        protected_attr = example.protected_attr
        if example_index < 5:
            logger.info("*** Example ***")
            logger.info("swag_id: {}".format(example.swag_id))
            for choice_idx, (tokens, input_ids, input_mask, segment_ids, vp_tokens, vp_input_ids, vp_input_mask) in enumerate(choices_features):
                logger.info("choice: {}".format(choice_idx))
                logger.info("tokens: {}".format(' '.join(tokens)))
                logger.info("input_ids: {}".format(' '.join(map(str, input_ids))))
                logger.info("input_mask: {}".format(' '.join(map(str, input_mask))))
                logger.info("segment_ids: {}".format(' '.join(map(str, segment_ids))))
            if is_training:
                logger.info("label: {}".format(label))
                logger.info("protected_attr: {}".format(protected_attr))

        features.append(
            InputFeatures(
                example_id = example.swag_id,
                choices_features = choices_features,
                label = label, 
                protected_attr = protected_attr,
                endings = example.endings
            )
        )

    return features

def _truncate_seq_pair(tokens_a, tokens_b, max_length):
    """Truncates a sequence pair in place to the maximum length."""

    # This is a simple heuristic which will always truncate the longer sequence
    # one token at a time. This makes more sense than truncating an equal percent
    # of tokens from each, since if one sequence is very short then each token
    # that's truncated likely contains more information than a longer sequence.
    while True:
        total_length = len(tokens_a) + len(tokens_b)
        if total_length <= max_length:
            break
        if len(tokens_a) > len(tokens_b):
            tokens_a.pop()
        else:
            tokens_b.pop()

def accuracy(out, labels):
    outputs = torch.argmax(out, dim=1)
    return torch.sum(outputs == labels)

def select_field(features, field):
    return [
        [
            choice[field]
            for choice in feature.choices_features
        ]
        for feature in features
    ]

def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--data_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The input data dir. Should contain the .csv files (or other data files) for the task.")
    parser.add_argument("--bert_model", default=None, type=str, required=True,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                        "bert-large-uncased, bert-base-cased, bert-large-cased, bert-base-multilingual-uncased, "
                        "bert-base-multilingual-cased, bert-base-chinese.")
    parser.add_argument("--output_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The output directory where the model checkpoints will be written.")

    ## Other parameters
    parser.add_argument("--max_seq_length",
                        default=128,
                        type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument("--do_train",
                        action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval",
                        action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_lower_case",
                        action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--train_batch_size",
                        default=32,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size",
                        default=8,
                        type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--learning_rate",
                        default=5e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs",
                        default=3.0,
                        type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion",
                        default=0.1,
                        type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    parser.add_argument("--no_cuda",
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--seed',
                        type=int,
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument('--fp16',
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--loss_scale',
                        type=float, default=0,
                        help="Loss scaling to improve fp16 numeric stability. Only used when fp16 set to True.\n"
                             "0 (default value): dynamic loss scaling.\n"
                             "Positive power of 2: static loss scaling value.\n")

    args = parser.parse_args()

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')
    logger.info("device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, bool(args.local_rank != -1), args.fp16))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
                            args.gradient_accumulation_steps))

    args.train_batch_size = args.train_batch_size // args.gradient_accumulation_steps

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_eval:
        raise ValueError("At least one of `do_train` or `do_eval` must be True.")

    #if os.path.exists(args.output_dir) and os.listdir(args.output_dir):
        #raise ValueError("Output directory ({}) already exists and is not empty.".format(args.output_dir))
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    tokenizer = BertTokenizer.from_pretrained(args.bert_model, do_lower_case=args.do_lower_case)

    train_examples = None
    num_train_optimization_steps = None
    if args.do_train:
        train_examples = read_swag_examples(os.path.join(args.data_dir, 'train.csv'), is_training = True)
        num_train_optimization_steps = int(
            len(train_examples) / args.train_batch_size / args.gradient_accumulation_steps) * args.num_train_epochs
        if args.local_rank != -1:
            num_train_optimization_steps = num_train_optimization_steps // torch.distributed.get_world_size()

    # Prepare model
    cache_dir = os.path.join(PYTORCH_PRETRAINED_BERT_CACHE, 'distributed_{}'.format(args.local_rank))
    predictor = BertForMultipleChoice.from_pretrained(args.bert_model,
        cache_dir=cache_dir,
        num_choices=4)
    adversary = BertForSequenceClassification.from_pretrained(args.bert_model,
              cache_dir=cache_dir,
              num_labels=3)
    
    if args.fp16:
        predictor.half()
        adversary.half()
    predictor.to(device)
    adversary.to(device)
    if args.local_rank != -1:
        try:
            from apex.parallel import DistributedDataParallel as DDP
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")

        predictor = DDP(predictor)
        adversary = DDP(adversary)
    elif n_gpu > 1:
        predictor = torch.nn.DataParallel(predictor)
        adversary = torch.nn.DataParallel(adversary)

    # Prepare optimizer
    param_optimizer_pred = list(predictor.named_parameters())
    param_optimizer_adv = list(adversary.named_parameters())
    
    # hack to remove pooler, which is not used
    # thus it produce None grad that break apex
    param_optimizer_pred = [n for n in param_optimizer_pred if 'pooler' not in n[0]]
    param_optimizer_adv = [n for n in param_optimizer_adv if 'pooler' not in n[0]]
    
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters_pred = [
        {'params': [p for n, p in param_optimizer_pred if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer_pred if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
    optimizer_grouped_parameters_adv = [
        {'params': [p for n, p in param_optimizer_adv if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer_adv if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
    if args.fp16:
        try:
            from apex.optimizers import FP16_Optimizer
            from apex.optimizers import FusedAdam
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")

        optimizer_pred = FusedAdam(optimizer_grouped_parameters_pred,
                              lr=args.learning_rate,
                              bias_correction=False,
                              max_grad_norm=1.0)
        optimizer_adv = FusedAdam(optimizer_grouped_parameters_adv,
                              lr=args.learning_rate,
                              bias_correction=False,
                              max_grad_norm=1.0)                      
        if args.loss_scale == 0:
            optimizer_pred = FP16_Optimizer(optimizer_pred, dynamic_loss_scale=True)
            optimizer_adv = FP16_Optimizer(optimizer_adv, dynamic_loss_scale=True)
        else:
            optimizer_pred = FP16_Optimizer(optimizer_pred, static_loss_scale=args.loss_scale)
            optimizer_adv = FP16_Optimizer(optimizer_adv, static_loss_scale=args.loss_scale)
    else:
        optimizer_pred = BertAdam(optimizer_grouped_parameters_pred,
                             lr=args.learning_rate,
                             warmup=args.warmup_proportion,
                             t_total=num_train_optimization_steps)
        optimizer_adv = BertAdam(optimizer_grouped_parameters_adv,
                             lr=args.learning_rate,
                             warmup=args.warmup_proportion,
                             t_total=num_train_optimization_steps)

    alpha = 1
    global_step = 0
    if args.do_train:
        train_features = convert_examples_to_features(
            train_examples, tokenizer, args.max_seq_length, True)
        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", num_train_optimization_steps)
        all_input_ids = torch.tensor(select_field(train_features, 'input_ids'), dtype=torch.long)
        all_input_mask = torch.tensor(select_field(train_features, 'input_mask'), dtype=torch.long)
        all_segment_ids = torch.tensor(select_field(train_features, 'segment_ids'), dtype=torch.long)
        all_label = torch.tensor([f.label for f in train_features], dtype=torch.long)
        all_vp_input_ids = torch.tensor(select_field(train_features, 'vp_input_ids'), dtype=torch.long)
        all_vp_input_mask = torch.tensor(select_field(train_features, 'vp_input_mask'), dtype=torch.long)
        all_protected_attr = torch.tensor([f.protected_attr for f in train_features], dtype=torch.long)
        train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label, all_vp_input_ids, all_vp_input_mask, all_protected_attr)
        if args.local_rank == -1:
            train_sampler = RandomSampler(train_data)
        else:
            train_sampler = DistributedSampler(train_data)
        train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)

        training_history = []
        predictor.train()
        adversary.train()
        for _ in trange(int(args.num_train_epochs), desc="Epoch"):
            tr_loss_pred, tr_loss_adv = 0, 0
            nb_tr_examples, nb_tr_steps = 0, 0
            for step, batch in enumerate(tqdm(train_dataloader, desc="Iteration")):
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_mask, segment_ids, label_ids, vp_input_ids, vp_input_mask, protected_attr_ids = batch
                loss_pred, logits = predictor(input_ids, segment_ids, input_mask, label_ids)
                max_prob, predicted_vps = torch.max(logits, dim=1)
                max_prob = (100 * torch.nn.functional.softmax(max_prob)).long().view([-1, 1])
                # print("predicted_vps: ", predicted_vps)
                predicted_vps = predicted_vps.view(-1, 1).repeat(1, vp_input_ids.size(2)).view([-1, 1, vp_input_ids.size(2)])
                vp_input_ids = torch.gather(vp_input_ids, dim=1, index=predicted_vps)
                vp_input_ids = vp_input_ids.view([vp_input_ids.size(0), -1])
                vp_input_ids = torch.cat((max_prob, vp_input_ids), dim=1)
                vp_input_mask = torch.gather(vp_input_mask, dim=1, index=predicted_vps)
                vp_input_mask = vp_input_mask.view([vp_input_mask.size(0), -1])
                vp_input_mask = torch.cat((0 * max_prob + 1, vp_input_mask), dim=1)
                print(vp_input_ids, vp_input_mask)
                # print(vp_input_ids.shape, vp_input_mask.shape, protected_attr_ids.shape)
                loss_adv, _ = adversary(vp_input_ids, None, vp_input_mask, protected_attr_ids)
                if n_gpu > 1:
                    loss_pred = loss_pred.mean() # mean() to average on multi-gpu.
                    loss_adv = loss_adv.mean()
                if args.fp16 and args.loss_scale != 1.0:
                    # rescale loss for fp16 training
                    # see https://docs.nvidia.com/deeplearning/sdk/mixed-precision-training/index.html
                    loss_pred = loss_pred * args.loss_scale
                    loss_adv = loss_adv * args.loss_scale
                if args.gradient_accumulation_steps > 1:
                    loss_pred = loss_pred / args.gradient_accumulation_steps
                    loss_adv = loss_adv / args.gradient_accumulation_steps
                tr_loss_pred += loss_pred.item()
                tr_loss_adv += loss_adv.item()
                nb_tr_examples += input_ids.size(0)
                nb_tr_steps += 1
                
                training_history.append([loss_pred.item(), loss_adv.item()])
                loss = loss_pred - alpha * loss_adv
                if args.fp16:
                    optimizer_pred.backward(loss)
                else:
                    loss.backward(retain_graph=True)
                # if (step + 1) % args.gradient_accumulation_steps == 0:
                if args.fp16:
                    # modify learning rate with special warm up BERT uses
                    # if args.fp16 is False, BertAdam is used that handles this automatically
                    lr_this_step = args.learning_rate * warmup_linear(global_step/num_train_optimization_steps, args.warmup_proportion)
                    for param_group in optimizer_pred.param_groups:
                        param_group['lr'] = lr_this_step
                    for param_group in optimizer_adv.param_groups:
                        param_group['lr'] = lr_this_step 
                optimizer_pred.step()
                optimizer_pred.zero_grad()
                optimizer_adv.zero_grad()
                
                if args.fp16:
                    optimizer_adv.backward(loss_adv)
                else:
                    loss_adv.backward()
                optimizer_adv.step()
                optimizer_adv.zero_grad()
                global_step += 1
        history_file = open(os.path.join(args.output_dir, "train_results.csv"), "w")
        writer = csv.writer(history_file, delimiter=",")
        writer.writerow(["pred_loss","adv_loss"])
        for row in training_history:
            writer.writerow(row)
    if args.do_train:
        # Save a trained model and the associated configuration
        model_to_save = predictor.module if hasattr(predictor, 'module') else predictor  # Only save the model it-self
        WEIGHTS_NAME = 'weights.pt'
        CONFIG_NAME = 'config.json'
        output_model_file = os.path.join(args.output_dir, 'predictor_' + WEIGHTS_NAME)
        torch.save(model_to_save.state_dict(), output_model_file)
        output_config_file = os.path.join(args.output_dir, 'predictor_' + CONFIG_NAME)
        with open(output_config_file, 'w') as f:
            f.write(model_to_save.config.to_json_string())
            
        # Load a trained model and config that you have fine-tuned
        config = BertConfig(output_config_file)
        predictor = BertForMultipleChoice(config, num_choices=4)
        predictor.load_state_dict(torch.load(output_model_file))
        
        # Do the same for adversary
        model_to_save = adversary.module if hasattr(adversary, 'module') else adversary  # Only save the model it-self
        output_model_file = os.path.join(args.output_dir, 'adversary_' + WEIGHTS_NAME)
        torch.save(model_to_save.state_dict(), output_model_file)
        output_config_file = os.path.join(args.output_dir, 'adversary_' + CONFIG_NAME)
        with open(output_config_file, 'w') as f:
            f.write(model_to_save.config.to_json_string())
            
        config = BertConfig(output_config_file)
        adversary = BertForSequenceClassification(config, num_labels=3)
        adversary.load_state_dict(torch.load(output_model_file))   
            
        
        
    else:
        WEIGHTS_NAME = 'weights.pt'
        CONFIG_NAME = 'config.json'
        output_model_file = os.path.join(args.output_dir, 'predictor_' + WEIGHTS_NAME)
        output_config_file = os.path.join(args.output_dir, 'predictor_' + CONFIG_NAME)
        config = BertConfig(output_config_file)
        predictor = BertForMultipleChoice(config, num_choices=4)
        predictor.load_state_dict(torch.load(output_model_file))
        
        output_model_file = os.path.join(args.output_dir, 'adversary_' + WEIGHTS_NAME)
        output_config_file = os.path.join(args.output_dir, 'adversary_' + CONFIG_NAME)
        config = BertConfig(output_config_file)
        adversary = BertForSequenceClassification(config, num_labels=3)
        adversary.load_state_dict(torch.load(output_model_file))        
        # predictor = BertForMultipleChoice.from_pretrained(args.bert_model, num_choices=4)
        # adversary = BertForSequenceClassification.from_pretrained(args.bert_model, num_labels=3)
    predictor.to(device)
    adversary.to(device)


    if args.do_eval and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
        eval_examples = read_swag_examples(os.path.join(args.data_dir, 'val.csv'), is_training = True)
        eval_features = convert_examples_to_features(
            eval_examples, tokenizer, args.max_seq_length, True)
        logger.info("***** Running evaluation *****")
        logger.info("  Num examples = %d", len(eval_examples))
        logger.info("  Batch size = %d", args.eval_batch_size)
        all_input_ids = torch.tensor(select_field(eval_features, 'input_ids'), dtype=torch.long)
        all_input_mask = torch.tensor(select_field(eval_features, 'input_mask'), dtype=torch.long)
        all_segment_ids = torch.tensor(select_field(eval_features, 'segment_ids'), dtype=torch.long)
        all_label = torch.tensor([f.label for f in eval_features], dtype=torch.long)
        all_vp_input_ids = torch.tensor(select_field(eval_features, 'vp_input_ids'), dtype=torch.long)
        all_vp_input_mask = torch.tensor(select_field(eval_features, 'vp_input_mask'), dtype=torch.long)
        all_protected_attr = torch.tensor([f.protected_attr for f in eval_features], dtype=torch.long)
        eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label, all_vp_input_ids, all_vp_input_mask, all_protected_attr)
        # Run prediction for full data
        eval_sampler = SequentialSampler(eval_data)
        eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

        predictor.eval()
        adversary.eval()
        eval_loss_pred, eval_accuracy_pred = 0, 0
        eval_loss_adv, eval_accuracy_adv = 0, 0
        nb_eval_steps, nb_eval_examples = 0, 0
        for input_ids, input_mask, segment_ids, label_ids, vp_input_ids, vp_input_mask, protected_attr_ids in eval_dataloader:
            input_ids = input_ids.to(device)
            input_mask = input_mask.to(device)
            segment_ids = segment_ids.to(device)
            label_ids = label_ids.to(device)
            vp_input_ids = vp_input_ids.to(device)
            vp_input_mask = vp_input_mask.to(device)
            protected_attr_ids = protected_attr_ids.to(device)
            
            with torch.no_grad():
                tmp_eval_loss_pred, logits_pred = predictor(input_ids, segment_ids, input_mask, label_ids)
            predicted_vps = torch.argmax(logits_pred, dim=1)    
            predicted_vps = predicted_vps.view(-1, 1).repeat(1, vp_input_ids.size(2)).view([-1, 1, vp_input_ids.size(2)])
            vp_input_ids = torch.gather(vp_input_ids, dim=1, index=predicted_vps)
            vp_input_ids = vp_input_ids.view([vp_input_ids.size(0), -1])
            vp_input_mask = torch.gather(vp_input_mask, dim=1, index=predicted_vps)
            vp_input_mask = vp_input_mask.view([vp_input_mask.size(0), -1])
            with torch.no_grad():
                tmp_eval_loss_adv, logits_adv = adversary(vp_input_ids, None, vp_input_mask, protected_attr_ids)
            
            # print("logits_adv", logits_adv)
            tmp_eval_accuracy_pred = accuracy(logits_pred, label_ids)
            tmp_eval_accuracy_adv = accuracy(logits_adv, protected_attr_ids)
            

            eval_loss_pred += tmp_eval_loss_pred.mean().item()
            eval_accuracy_pred += tmp_eval_accuracy_pred.item()
            eval_loss_adv += tmp_eval_loss_adv.mean().item()
            eval_accuracy_adv += tmp_eval_accuracy_adv.item()

            nb_eval_examples += input_ids.size(0)
            nb_eval_steps += 1

        eval_loss_pred /= nb_eval_steps
        eval_accuracy_pred /= nb_eval_examples
        eval_loss_adv /= nb_eval_steps
        eval_accuracy_adv /= nb_eval_examples
        
        if args.do_train:
            result = {'eval_loss_pred': eval_loss_pred,
                      'eval_accuracy_pred': eval_accuracy_pred,
                      'eval_loss_adv': eval_loss_adv,
                      'eval_accuracy_adv': eval_accuracy_adv,
                      'global_step': global_step,
                      'loss_pred': tr_loss_pred/nb_tr_steps,
                      'loss_adv': tr_loss_adv/nb_tr_steps}
        else:
            result = {'eval_loss_pred': eval_loss_pred,
                      'eval_accuracy_pred': eval_accuracy_pred,
                      'eval_loss_adv': eval_loss_adv,
                      'eval_accuracy_adv': eval_accuracy_adv}

        output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
        with open(output_eval_file, "w") as writer:
            logger.info("***** Eval results *****")
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))


if __name__ == "__main__":
    main()
