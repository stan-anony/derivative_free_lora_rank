import os
import time
import pickle
import random

import torch
# import fitlog
import argparse
import numpy as np
import cma
from fastNLP import cache_results, Tester, DataSet
from transformers import (
    RobertaConfig,
    RobertaTokenizer,
    BertConfig,
    BertTokenizer,
    BartConfig,
    BartTokenizer,
    T5Config,
    T5Tokenizer,
    GPT2Config,
    GPT2Tokenizer,
)
from models.deep_modeling_roberta import RobertaForMaskedLM
#from models.deep_modeling_bart import BartForConditionalGeneration
#from models.deep_modeling_t5 import T5ForConditionalGeneration
from models.modeling_gpt2 import GPT2LMHeadModel
#from models.deep_modeling_bert import BertForMaskedLM
#from models.deep_modeling_cpt import CPTForMaskedLM
from utils import hinge_loss
from sklearn.metrics import f1_score
#from models.modeling_llama import LlamaForCausalLM
import deepspeed

parser = argparse.ArgumentParser()
parser.add_argument("--model_name", default='roberta-large',
                    choices=['roberta-base', 'roberta-large',
                             'bert-base-uncased', 'bert-large-uncased',
                             'facebook/bart-base', 'facebook/bart-large',
                             't5-small', 't5-base', 't5-large', 't5-3b',
                             'gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl',
                             'fnlp/cpt-large','llama',
                             ], type=str)
parser.add_argument("--task_name", default='sst2', type=str)
parser.add_argument("--intrinsic_dim", default=500, type=int)
parser.add_argument("--k_shot", default=16, type=int)
parser.add_argument("--batch_size", default=16, type=int)
parser.add_argument("--budget", default=8000, type=int)
parser.add_argument("--popsize", default=20, type=int)
parser.add_argument("--bound", default=0, type=int)
parser.add_argument("--sigma", default=1, type=float)
parser.add_argument("--alpha", default=1, type=float)
parser.add_argument("--print_every", default=50, type=int)
parser.add_argument("--eval_every", default=100, type=int)
parser.add_argument("--device", default='cuda', type=str)
parser.add_argument("--alg", default='CMA', type=str)
parser.add_argument("--random_proj", default='normal', type=str)
parser.add_argument("--seed", default=42, type=int)
parser.add_argument("--loss_type", default='ce', type=str)
parser.add_argument("--lora_r", default=4, type=int)
parser.add_argument("--local_rank", default=1, type=int)
parser.add_argument("--world_size", default=1, type=int)
parser.add_argument("--deepspeed_config", default=None, type=str)
parser.add_argument(
    "--inference_framework",
    default='pt',
    type=str,
    help='''Which inference framework to use. 
         Currently supports `pt` and `ort`, standing for pytorch and Microsoft onnxruntime respectively'''
)
parser.add_argument(
    "--onnx_model_path",
    default=None,
    type=str,
    help='Path to your onnx model.'
)
args = parser.parse_args()
local_rank = args.local_rank
world_size = int(os.getenv("WORLD_SIZE", "1"))
deepspeed.init_distributed()

# below are free hyper-params
model_name = args.model_name
if model_name in ['t5-small', 't5-base', 't5-large', 't5-3b']:
    from dataloaders.dataloader_t5 import SST2Loader, AGNewsLoader, YelpPLoader, DBPediaLoader, RTELoader, MRPCLoader, SNLILoader
    from metrics.metrics_t5 import SST2Metric, AGNewsMetric, YelpPMetric, DBPediaMetric, RTEMetric, MRPCMetric, SNLIMetric
elif model_name in ['gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl','llama']:
    from dataloaders.dataloader_gpt import SST2Loader, AGNewsLoader, YelpPLoader, DBPediaLoader, RTELoader, MRPCLoader, SNLILoader
    from metrics.metrics_gpt import SST2Metric, AGNewsMetric, YelpPMetric, DBPediaMetric, RTEMetric, MRPCMetric, SNLIMetric
elif model_name in ['fnlp/cpt-large']:
    from dataloaders.dataloader_cpt import ChnSentLoader, AmazonLoader, THUCNewsLoader, BQLoader, CMNLILoader, CCPMLoader, \
        TNewsLoader, \
        OCNLILoader, LCQMCLoader, C3Loader
    from metrics.metrics_cpt import ChnSentMetric, AmazonMetric, THUCNewsMetric, BQMetric, CMNLIMetric, CCPMMetric, TNewsMetric, \
        OCNLIMetric, LCQMCMetric, C3Metric
else:
    from dataloaders.dataloader import SST2Loader, AGNewsLoader, YelpPLoader, DBPediaLoader, RTELoader, MRPCLoader, SNLILoader
    from metrics.metrics import SST2Metric, AGNewsMetric, YelpPMetric, DBPediaMetric, RTEMetric, MRPCMetric, SNLIMetric

task_name = args.task_name
intrinsic_dim = args.intrinsic_dim
k_shot = args.k_shot
batch_size = args.batch_size
lora_r = args.lora_r
#fix the following code
budget = args.budget
bound = args.bound
sigma = args.sigma
alpha = args.alpha
if args.popsize > 0:
    popsize = args.popsize
else:
    popsize = 4 + 3 * np.log(intrinsic_dim)
device = args.device
alg = args.alg
random_proj = args.random_proj
seed = args.seed
loss_type = args.loss_type
print_every = args.print_every
eval_every = args.eval_every
# if task_name in ['mrpc', 'snli', 'qnli', 'rte']:
#     args.cat_or_add = 'cat'
inference_framework = args.inference_framework
onnx_model_path = args.onnx_model_path
save_hiddens = False


if task_name in ['sst2', 'yelpp', 'rte', 'mrpc', 'chnsent', 'lcqmc', 'bq']:
    num_labels = 2
elif task_name in ['snli', 'cmnli', 'ocnli']:
    num_labels = 3
elif task_name in ['agnews', 'ccpm', 'c3']:
    num_labels = 4
elif task_name in ['amazon']:
    num_labels = 5
elif task_name in ['thucnews']:
    num_labels = 10
elif task_name in ['dbpedia', 'tnews']:
    num_labels = 14
else:
    raise ValueError

# save_path = 'deep_{}_results/{}_results/D_{}_d_{}_data_{}_{}_range_{}_loss_{}_budget_{}_seed_{}_{}_{}_{}'.format(
#     model_name.replace('/', '-'),
#     task_name,
#     n_prompt_tokens * 1024,
#     intrinsic_dim,
#     k_shot * num_labels,
#     alg,
#     bound,
#     loss_type,
#     budget,
#     seed,
#     cat_or_add,
#     random_proj,
#     inference_framework
# )
# print('Results will be saved in {}'.format(save_path))
#
# if os.path.exists(save_path):
#     print('Experiment already run.')
#     exit()
#
# args.save_path = save_path
args.bbt_version = 'deepbbt'

# log_dir = './logs'
# fitlog.set_log_dir(log_dir)
# fitlog.commit(__file__, fit_msg=save_path)
# fitlog.add_hyper(args)
# fitlog.add_hyper_in_file(__file__)

random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)


class LMForwardAPI:
    def __init__(self, model_name='roberta-large', task_name='sst2',
                 loss_type='hinge', lora_r =4):
        self.model_name = model_name
        self.lora_r = lora_r
        if model_name in ['roberta-base', 'roberta-large']:
            self.config = RobertaConfig.from_pretrained("./roberta-large")
            self.tokenizer = RobertaTokenizer.from_pretrained("./roberta-large")
            self.model = RobertaForMaskedLM.from_pretrained(
                model_name,
                config=self.config,
                lora_r=lora_r,
                inference_framework=inference_framework,
                onnx_model_path=onnx_model_path,
            )
            self.model.lm_head.bias = torch.nn.parameter.Parameter(torch.zeros(self.config.vocab_size))
        elif model_name in ['bert-base-uncased', 'bert-large-uncased']:
            self.config = BertConfig.from_pretrained(model_name)
            self.tokenizer = BertTokenizer.from_pretrained(model_name)
            self.model = BertForMaskedLM.from_pretrained(
                model_name,
                config=self.config,
                lora_r=lora_r,
            )
        elif model_name in ['facebook/bart-base', 'facebook/bart-large']:
            self.config = BartConfig.from_pretrained(model_name)
            self.tokenizer = BartTokenizer.from_pretrained(model_name)
            self.model = BartForConditionalGeneration.from_pretrained(
                model_name,
                config=self.config,
                lora_r=lora_r,
            )
        elif model_name in ['t5-small', 't5-base', 't5-large', 't5-3b']:
            self.config = T5Config.from_pretrained(model_name)
            self.tokenizer = T5Tokenizer.from_pretrained(model_name)
            self.model = T5ForConditionalGeneration.from_pretrained(
                model_name,
                config=self.config,
                lora_r=lora_r,
            )
        elif model_name in ['gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl']:
            self.config = GPT2Config.from_pretrained("./gpt2-xl")
            self.tokenizer = GPT2Tokenizer.from_pretrained("./gpt2-xl")
            model_name_or_path = "./gpt2-xl"
            self.model = GPT2LMHeadModel.from_pretrained(
                model_name_or_path,
                config=self.config,
            )
        elif model_name in ['fnlp/cpt-large']:
            self.config = BartConfig.from_pretrained(model_name)
            self.tokenizer = BertTokenizer.from_pretrained(model_name)
            self.model = CPTForMaskedLM.from_pretrained(
                model_name,
                config=self.config,
                lora_r=lora_r,
            )
        elif model_name in ['llama']:
            self.config = LlamaConfig.from_pretrained("../llama_model/llama2/llama-2-7b")
            self.tokenizer = LlamaTokenizer.from_pretrained("../llama_model/llama2/llama-2-7b")
            model_name_or_path = "../llama_model/llama2/llama-2-7b"
            self.model = LlamaForCausalLM.from_pretrained(
                model_name_or_path,
                config=self.config,
            )
        else:
            raise NotImplementedError

        if random_proj == 'normal':
            self.config.output_hidden_states = True

        if inference_framework == 'ort':
            self.model.roberta = None
        '''
        self.best_prefix = torch.zeros(self.config.num_hidden_layers, n_prompt_tokens, self.config.hidden_size,
                                       device=device)
        '''
        self.best_lora_A = torch.zeros(self.config.num_hidden_layers, self.config.hidden_size, lora_r,
                                       device=device)
        self.best_lora_B = torch.zeros(self.config.num_hidden_layers,  lora_r, self.config.hidden_size,
                                       device=device)
        self.best_lora_KA = torch.zeros(self.config.num_hidden_layers, self.config.hidden_size, lora_r,
                                       device=device)
        self.best_lora_KB = torch.zeros(self.config.num_hidden_layers,  lora_r, self.config.hidden_size,
                                       device=device)
        #self.best = None
        self.best_A = None
        self.best_B = None
        self.best_KA = None
        self.best_KB = None
        '''
        self.linear = torch.nn.ModuleList(
            [torch.nn.Linear(intrinsic_dim, n_prompt_tokens * self.config.hidden_size, bias=False) for _ in
             range(self.config.num_hidden_layers)])
        '''
        self.lora_A_linear = torch.nn.ModuleList(
            [torch.nn.Linear(int(intrinsic_dim/4), self.config.hidden_size * lora_r, bias=False,device=device) for _ in
             range(self.config.num_hidden_layers)])
        self.lora_B_linear = torch.nn.ModuleList(
            [torch.nn.Linear(int(intrinsic_dim/4), lora_r * self.config.hidden_size, bias=False,device=device) for _ in
             range(self.config.num_hidden_layers)])
        self.lora_KA_linear = torch.nn.ModuleList(
            [torch.nn.Linear(int(intrinsic_dim/4), self.config.hidden_size * lora_r, bias=False,device=device) for _ in
             range(self.config.num_hidden_layers)])
        self.lora_KB_linear = torch.nn.ModuleList(
            [torch.nn.Linear(int(intrinsic_dim/4), lora_r * self.config.hidden_size, bias=False,device=device) for _ in
             range(self.config.num_hidden_layers)])
        if random_proj == 'normal':
            # calculate std for normal distribution
            if model_name in ['roberta-base', 'roberta-large']:
                embedding = self.model.roberta.get_input_embeddings().weight.clone().cpu()
            elif model_name in ['bert-base-uncased', 'bert-large-uncased']:
                embedding = self.model.bert.get_input_embeddings().weight.clone().cpu()
            elif model_name in ['facebook/bart-base', 'facebook/bart-large', 'fnlp/cpt-large']:
                embedding = self.model.model.get_input_embeddings().weight.clone().cpu()
            elif model_name in ['gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl']:
                embedding = self.model.transformer.get_input_embeddings().weight.clone().cpu()
            elif model_name in ['t5-small', 't5-base', 't5-large', 't5-3b']:
                embedding = self.model.get_input_embeddings().weight.clone().cpu()
            else:  #llama
                embedding = self.model.model.get_input_embeddings().weight.clone().cpu()

            # embedding = embedding[1000: 2000]
            mu_hat = np.mean(embedding.reshape(-1).detach().cpu().numpy())
            std_hat = np.std(embedding.reshape(-1).detach().cpu().numpy())
            # temp = intrinsic_dim - std_hat * std_hat
            # mu = mu_hat / temp
            # std = std_hat / np.sqrt(temp)
            mu = 0.0
            std = alpha * std_hat / (np.sqrt(intrinsic_dim) * sigma)
            print('[Embedding] mu: {} | std: {} [RandProj]  mu: {} | std: {}'.format(mu_hat, std_hat, mu, std))
            '''
            for p in self.linear[0].parameters():
                torch.nn.init.normal_(p, 0.0, std)
            '''   
            for p in self.lora_A_linear[0].parameters():
                torch.nn.init.normal_(p, 0.0, std)
            for p in self.lora_B_linear[0].parameters():
                torch.nn.init.normal_(p, 0.0, std)
            for p in self.lora_KA_linear[0].parameters():
                torch.nn.init.normal_(p, 0.0, std)
            for p in self.lora_KB_linear[0].parameters():
                torch.nn.init.normal_(p, 0.0, std)
            self.intermediate_stats = [(mu, std)]
            
        ds_engine = deepspeed.init_inference(self.model,mp_size=world_size,
                                           dtype=torch.float,
                                           replace_method="auto",
                                           replace_with_kernel_inject=False)
        ds_engine.to(device)
        self.model = ds_engine.module                                  
        #self.model.to(device)
        self.model.eval()
        self.best_train_perf = 0.0
        self.best_dev_perf = 0.0
        self.num_call = 0
        # self.save_path = save_path
        self.print_every = print_every
        self.eval_every = eval_every
        self.loss_type = loss_type
        # if save_path is not None:
        #     os.makedirs(save_path, exist_ok=True)
        if task_name == 'sst2':
            self.metric = SST2Metric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'SST2Metric'
        elif task_name == 'agnews':
            self.metric = AGNewsMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'AGNewsMetric'
        elif task_name == 'yelpp':
            self.metric = YelpPMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'YelpPMetric'
        elif task_name == 'dbpedia':
            self.metric = DBPediaMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'DBPediaMetric'
        elif task_name == 'rte':
            self.metric = RTEMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'RTEMetric'
        elif task_name == 'mrpc':
            self.metric = MRPCMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'f1'
            self.metric_name = 'MRPCMetric'
        elif task_name == 'snli':
            self.metric = SNLIMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'SNLIMetric'
        elif task_name == 'chnsent':
            self.metric = ChnSentMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'ChnSentMetric'
        elif task_name == 'thucnews':
            self.metric = THUCNewsMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'THUCNewsMetric'
        elif task_name == 'lcqmc':
            self.metric = LCQMCMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'LCQMCMetric'
        elif task_name == 'cmnli':
            self.metric = CMNLIMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'CMNLIMetric'
        elif task_name == 'ocnli':
            self.metric = OCNLIMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'OCNLIMetric'
        elif task_name == 'amazon':
            self.metric = AmazonMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'AmazonMetric'
        elif task_name == 'bq':
            self.metric = BQMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'BQMetric'
        elif task_name == 'ccpm':
            self.metric = CCPMMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'CCPMMetric'
        elif task_name == 'tnews':
            self.metric = TNewsMetric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'TNewsMetric'
        elif task_name == 'c3':
            self.metric = C3Metric(target='labels', pred='logits', tokenizer=tokenizer)
            self.metric_key = 'acc'
            self.metric_name = 'C3Metric'
        else:
            raise NotImplementedError
        self.margin = self.metric.margin
        self.ce_loss = torch.nn.CrossEntropyLoss(reduction='mean')

    def calc_metric(self, logits, target):
        label_map = self.metric.label_map

        converted_target = target.clone()
        for key, val in label_map.items():
            converted_target[target == key] = val
        interest_index = list(label_map.keys())
        logits = logits[:, interest_index]
        pred = logits.argmax(dim=-1)

        if self.metric_key == 'acc':
            perf = (pred == converted_target).sum() / len(target)
        elif self.metric_key == 'f1':
            perf = f1_score(converted_target.detach().cpu().numpy().tolist(),
                            pred.detach().cpu().numpy().tolist())
        else:
            raise KeyError(f'[Metric] Only support [acc, f1], got {self.metric_key} instead.')

        if self.loss_type == 'hinge':
            loss = hinge_loss(logits, converted_target, margin=self.margin, reduction='sum').item() / len(target)
        elif self.loss_type == 'ce':
            loss = self.ce_loss(logits, converted_target).item()
        elif self.loss_type == 'perf':
            loss = -1 * perf
        else:
            raise KeyError(f'[Loss] Only support [hinge, ce, perf], got {self.loss_type} instead.')

        return loss, perf

    def eval(self, lora_embedding=None, layer_id=None, flag=None,test_data=None):
        with torch.autocast("cuda"):
            self.num_call += 1
            #best_prefix = self.best_prefix.clone()
            best_lora_A = self.best_lora_A.clone()
            best_lora_B = self.best_lora_B.clone()
            best_lora_KA = self.best_lora_KA.clone()
            best_lora_KB = self.best_lora_KB.clone()
            lora_r = self.lora_r
            '''
            if prompt_embedding is not None:
                prompt_embedding = torch.tensor(prompt_embedding).type(torch.float32)  # z
                prompt_embedding = self.linear[layer_id](prompt_embedding).reshape(-1, self.config.hidden_size)  # Az
                best_prefix[layer_id] = prompt_embedding
            '''
            size = int(intrinsic_dim/4)
            if lora_embedding is not None:
                lora_QA_embedding = lora_embedding[:size]
                lora_QB_embedding = lora_embedding[size:2*size]
                lora_KKA_embedding = lora_embedding[2*size:3*size]
                lora_KKB_embedding = lora_embedding[3*size:]

                lora_QA_embedding = torch.tensor(lora_QA_embedding,device=device).type(torch.float32)
                lora_QB_embedding = torch.tensor(lora_QB_embedding,device=device).type(torch.float32)
                lora_KKA_embedding = torch.tensor(lora_KKA_embedding,device=device).type(torch.float32)
                lora_KKB_embedding = torch.tensor(lora_KKB_embedding,device=device).type(torch.float32)

                lora_A_embedding = self.lora_A_linear[layer_id](lora_QA_embedding).reshape(self.config.hidden_size, -1)
                best_lora_A[layer_id] = lora_A_embedding 

                lora_B_embedding = self.lora_B_linear[layer_id](lora_QB_embedding).reshape(-1, self.config.hidden_size)
                best_lora_B[layer_id] = lora_B_embedding

                lora_KA_embedding = self.lora_KA_linear[layer_id](lora_KKA_embedding).reshape(self.config.hidden_size, -1)
                best_lora_KA[layer_id] = lora_KA_embedding 

                lora_KB_embedding = self.lora_KB_linear[layer_id](lora_KKB_embedding).reshape(-1, self.config.hidden_size)
                best_lora_KB[layer_id] = lora_KB_embedding
            #self.model.set_prompt_embedding(best_prefix)
            self.model.set_lora_A_embedding(best_lora_A)
            self.model.set_lora_B_embedding(best_lora_B)
            self.model.set_lora_KA_embedding(best_lora_KA)
            self.model.set_lora_KB_embedding(best_lora_KB)
            if isinstance(test_data, DataSet):
                #self.model.set_prompt_embedding(self.best)
                self.model.set_lora_A_embedding(self.best_A)
                self.model.set_lora_B_embedding(self.best_B)
                self.model.set_lora_KA_embedding(self.best_KA)
                self.model.set_lora_KB_embedding(self.best_KB)
                test_tester = Tester(data=test_data, model=self.model, metrics=self.metric, batch_size=batch_size,
                                    num_workers=1, device=device, use_tqdm=True)
                results = test_tester.test()
                test_acc = results[self.metric_name][self.metric_key]
                # fitlog.add_best_metric(test_acc, name='test_acc')
                return test_acc
            else:
                for k, v in train_data.items():
                    train_data[k] = v.to(device)
                with torch.no_grad():
                    if model_name in ['t5-small', 't5-base', 't5-large', 't5-3b']:
                        outputs = self.model(
                            input_ids=train_data['input_ids'],
                            attention_mask=train_data['attention_mask'],
                            decoder_input_ids=train_data['decoder_input_ids'],
                            decoder_attention_mask=train_data['decoder_attention_mask'],
                        )
                    elif model_name in ['gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl','llama']:
                        outputs = self.model(
                            input_ids=train_data['input_ids'],
                            attention_mask=train_data['attention_mask'],
                        )
                    else:
                        outputs = self.model(
                            input_ids=train_data['input_ids'],
                            attention_mask=train_data['attention_mask'],
                            mask_pos=train_data['mask_pos'],
                        )
                    logits = outputs['logits']
                    if random_proj == 'normal' and len(self.intermediate_stats) == 1:
                        # if is the first forward pass, record the range of hidden states of each layer
                        print('Calculating std for random projections...')
                        if self.model_name in ['facebook/bart-base', 'facebook/bart-large',
                                            't5-small', 't5-base', 't5-large', 't5-3b',
                                            'fnlp/cpt-large',
                                            ]:
                            hidden_states = outputs['encoder_hidden_states']
                        else:
                            hidden_states = outputs['hidden_states']
                        for i, h in enumerate(hidden_states[1:-1]):
                            if save_hiddens:
                                hid_path = './hidstates/{}'.format(self.model_name.split('/')[-1])
                                if not os.path.exists(hid_path):
                                    os.makedirs(hid_path, exist_ok=True)
                                with open('{}/hidden_{}.bin'.format(hid_path, i + 1), 'wb') as f:
                                    pickle.dump(h, f)
                            print('[Layer {}]'.format(i + 1))
                            hidden = h.clone().reshape(-1).detach().cpu().numpy()
                            mu_hat = np.mean(hidden)
                            std_hat = np.std(hidden)
                            max_h = np.max(hidden)
                            min_h = np.min(hidden)
                            print(' - Before clipping: mu=%.4f, std=%.4f, min=%.4f, max=%.4f' % (
                                mu_hat, std_hat, min_h, max_h))
                            # Clipping outliers
                            clip_round = 0
                            while clip_round < 5:
                                clip_round += 1
                                min_bound = mu_hat - 3 * std_hat
                                max_bound = mu_hat + 3 * std_hat
                                hidden = np.clip(hidden, min_bound, max_bound)
                                mu_hat = np.mean(hidden)
                                std_hat = np.std(hidden)
                                max_h = np.max(hidden)
                                min_h = np.min(hidden)
                                print(' - After clipping (round %d): mu=%.4f, std=%.4f, min=%.4f, max=%.4f' % (
                                    clip_round, mu_hat, std_hat, min_h, max_h))
                            # Calculating std dev for the random projection
                            mu = 0.0
                            std = alpha * std_hat / (np.sqrt(intrinsic_dim) * sigma)
                            # temp = intrinsic_dim - std_hat * std_hat
                            # mu = mu_hat / temp
                            # std = std_hat / np.sqrt(temp)
                            print(' - Random Projection: mu=%.4f, std=%.4f' % (mu, std))
                            for p in self.lora_A_linear[i + 1].parameters():
                                torch.nn.init.normal_(p, mu, std)
                            '''
                            for p in self.lora_B_linear[i + 1].parameters():
                                torch.nn.init.normal_(p, mu, std)
                            '''
                            for p in self.lora_KA_linear[i + 1].parameters():
                                torch.nn.init.normal_(p, mu, std)
                            '''
                            for p in self.lora_KB_linear[i + 1].parameters():
                                torch.nn.init.normal_(p, mu, std)
                            '''
                            self.intermediate_stats.append((mu, std))
                        assert len(self.intermediate_stats) == self.config.num_hidden_layers
                        self.model.config.output_hidden_states = None
                        print('Random projections initialized.')

                loss, perf = self.calc_metric(logits, train_data['labels'])
                # fitlog.add_loss(loss, name=self.loss_type, step=self.num_call)
                # fitlog.add_metric(perf, name='train_acc', step=self.num_call)

                if perf > self.best_train_perf:
                    self.best_train_perf = perf
                    # fitlog.add_best_metric(self.best_train_perf, name='train_acc')

                # if self.save_path is not None:
                #     with open(os.path.join(self.save_path, 'train_acc.txt'), 'a') as fout:
                #         fout.write('{}\t{}\t{}\n'.format(self.num_call, loss, perf))

                if self.num_call % self.print_every == 0:
                    print(
                        '[# API Calls {}] loss: {}. Current perf: {}. Best perf so far: {}'.format(
                            self.num_call,
                            round(float(loss), 4),
                            round(float(perf), 4),
                            round(float(self.best_train_perf), 4)))

                if self.num_call % self.eval_every == 0:
                    print('********* Evaluated on dev set *********')
                    for k, v in dev_data.items():
                        dev_data[k] = v.to(device)
                    with torch.no_grad():
                        if model_name in ['t5-small', 't5-base', 't5-large', 't5-3b']:
                            logits = self.model(
                                input_ids=dev_data['input_ids'],
                                attention_mask=dev_data['attention_mask'],
                                decoder_input_ids=dev_data['decoder_input_ids'],
                                decoder_attention_mask=dev_data['decoder_attention_mask'],
                            )['logits']
                        elif model_name in ['gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl','llama']:
                            logits = self.model(
                                input_ids=dev_data['input_ids'],
                                attention_mask=dev_data['attention_mask'],
                            )['logits']
                        else:
                            logits = self.model(
                                input_ids=dev_data['input_ids'],
                                attention_mask=dev_data['attention_mask'],
                                mask_pos=dev_data['mask_pos'],
                            )['logits']

                    dev_loss, dev_perf = self.calc_metric(logits, dev_data['labels'])
                    # fitlog.add_metric(dev_perf, name='dev_acc', step=self.num_call)
                    if dev_perf > self.best_dev_perf:
                        self.best_dev_perf = dev_perf
                        # fitlog.add_best_metric(self.best_dev_perf, name='dev_acc')
                        #self.best = best_prefix.clone()
                        self.best_A = best_lora_A.clone()
                        self.best_B = best_lora_B.clone()
                        self.best_KA = best_lora_KA.clone()
                        self.best_KB = best_lora_KB.clone()
                    # if self.save_path is not None:
                    #     with open(os.path.join(self.save_path, 'dev_loss.txt'), 'a') as fout:
                    #         fout.write('{}\t{}\t{}\n'.format(self.num_call, dev_loss, dev_perf))
                    print('Dev loss: {}. Dev perf: {}. Best dev perf: {}'.format(
                        round(float(dev_loss), 4),
                        round(float(dev_perf), 4),
                        round(float(self.best_dev_perf), 4)))
                    print('********* Done *********')
                return loss


if model_name in ['roberta-base', 'roberta-large']:
    tokenizer = RobertaTokenizer.from_pretrained("./roberta-large")
elif model_name in ['bert-base-uncased', 'bert-large-uncased', 'fnlp/cpt-large']:
    tokenizer = BertTokenizer.from_pretrained(model_name)
elif model_name in ['facebook/bart-base', 'facebook/bart-large']:
    tokenizer = BartTokenizer.from_pretrained(model_name)
elif model_name in ['t5-small', 't5-base', 't5-large', 't5-3b']:
    tokenizer = T5Tokenizer.from_pretrained(model_name)
elif model_name in ['gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl']:
    tokenizer = GPT2Tokenizer.from_pretrained("./gpt2-xl")
elif model_name in ['llama']:
    tokenizer = LlamaTokenizer.from_pretrained("../llama_model/llama2/llama-2-7b")
else:
    raise NotImplementedError

cache_fn = f"caches/data_{model_name.replace('/', '-')}_{task_name}_{lora_r}_{seed}.pt"
if model_name not in ['fnlp/cpt-large']:
    DataLoader = {
        'sst2': SST2Loader,
        'agnews': AGNewsLoader,
        'yelpp': YelpPLoader,
        'dbpedia': DBPediaLoader,
        'rte': RTELoader,
        'mrpc': MRPCLoader,
        'snli': SNLILoader,
    }
else:
    DataLoader = {
        'chnsent': ChnSentLoader,
        'thucnews': THUCNewsLoader,
        'lcqmc': LCQMCLoader,
        'cmnli': CMNLILoader,
        'ocnli': OCNLILoader,
        'amazon': AmazonLoader,
        'bq': BQLoader,
        'ccpm': CCPMLoader,
        'tnews': TNewsLoader,
        'c3': C3Loader,
    }


@cache_results(cache_fn, _refresh=False)
def get_data(task_name, tokenizer):
    if task_name in ['agnews', 'yelpp', 'dbpedia', 'snli']:
        splits = ['train', 'test']
    else:  # for datasets without test set, we use dev set
        splits = ['train', 'validation']
    data_bundle = DataLoader[task_name](tokenizer=tokenizer).my_load(splits)
    return data_bundle


def construct_true_few_shot_data(train_data, k_shot):
    train_label_count = {}
    dev_label_count = {}
    new_train_data = DataSet()
    new_dev_data = DataSet()
    all_indices = [_ for _ in range(len(train_data))]
    np.random.shuffle(all_indices)

    for index in all_indices:
        label = train_data[index]['labels']
        if label < 0:
            continue

        if label not in train_label_count:
            train_label_count[label] = 0
        if label not in dev_label_count:
            dev_label_count[label] = 0

        if train_label_count[label] < k_shot:
            new_train_data.append(train_data[index])
            train_label_count[label] += 1
        elif dev_label_count[label] < k_shot:
            new_dev_data.append(train_data[index])
            dev_label_count[label] += 1

    if model_name in ['t5-small', 't5-base', 't5-large', 't5-3b']:
        new_train_data.set_input("input_ids", "attention_mask", "decoder_input_ids", "decoder_attention_mask")
        new_dev_data.set_input("input_ids", "attention_mask", "decoder_input_ids", "decoder_attention_mask")
    elif model_name in ['gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl','llama']:
        new_train_data.set_input("input_ids", "attention_mask")
        new_dev_data.set_input("input_ids", "attention_mask")
    else:
        new_train_data.set_input("input_ids", "attention_mask", "mask_pos")
        new_dev_data.set_input("input_ids", "attention_mask", "mask_pos")

    new_train_data.set_target("labels")
    new_dev_data.set_target("labels")
    return new_train_data, new_dev_data


data_bundle = get_data(task_name=task_name, tokenizer=tokenizer)
if task_name in ['agnews', 'yelpp', 'dbpedia', 'snli']:
    train_data, test_data = data_bundle.get_dataset('train'), data_bundle.get_dataset('test')
else:
    train_data, test_data = data_bundle.get_dataset('train'), data_bundle.get_dataset('validation')

train_data, dev_data = construct_true_few_shot_data(train_data, k_shot)

for ds in [train_data, dev_data, test_data]:
    ds.set_pad_val('input_ids', tokenizer.pad_token_id if tokenizer.pad_token_id is not None else 0)
    ds.set_pad_val('attention_mask', 0)

print('# of train data: {}'.format(len(train_data)))
print('Example:')
print(train_data[0])
print('\n# of dev data: {}'.format(len(dev_data)))
print('Example:')
print(dev_data[0])
print('\n# of test data: {}'.format(len(test_data)))
print('Example:')
print(test_data[0])

if model_name in ['t5-small', 't5-base', 't5-large', 't5-3b']:
    train_data = {
        'input_ids': torch.tensor(train_data['input_ids'].get(list(range(len(train_data))))),
        'attention_mask': torch.tensor(train_data['attention_mask'].get(list(range(len(train_data))))),
        'decoder_input_ids': torch.tensor(train_data['decoder_input_ids'].get(list(range(len(train_data))))),
        'decoder_attention_mask': torch.tensor(train_data['decoder_attention_mask'].get(list(range(len(train_data))))),
        'labels': torch.tensor(train_data['labels'].get(list(range(len(train_data))))),
    }
    dev_data = {
        'input_ids': torch.tensor(dev_data['input_ids'].get(list(range(len(dev_data))))),
        'attention_mask': torch.tensor(dev_data['attention_mask'].get(list(range(len(dev_data))))),
        'decoder_input_ids': torch.tensor(dev_data['decoder_input_ids'].get(list(range(len(dev_data))))),
        'decoder_attention_mask': torch.tensor(dev_data['decoder_attention_mask'].get(list(range(len(dev_data))))),
        'labels': torch.tensor(dev_data['labels'].get(list(range(len(dev_data))))),
    }
elif model_name in ['gpt2', 'gpt2-medium', 'gpt2-large', 'gpt2-xl','llama']:
    train_data = {
        'input_ids': torch.tensor(train_data['input_ids'].get(list(range(len(train_data))))),
        'attention_mask': torch.tensor(train_data['attention_mask'].get(list(range(len(train_data))))),
        'labels': torch.tensor(train_data['labels'].get(list(range(len(train_data))))),
    }
    dev_data = {
        'input_ids': torch.tensor(dev_data['input_ids'].get(list(range(len(dev_data))))),
        'attention_mask': torch.tensor(dev_data['attention_mask'].get(list(range(len(dev_data))))),
        'labels': torch.tensor(dev_data['labels'].get(list(range(len(dev_data))))),
    }
else:
    train_data = {
        'input_ids': torch.tensor(train_data['input_ids'].get(list(range(len(train_data))))),
        'attention_mask': torch.tensor(train_data['attention_mask'].get(list(range(len(train_data))))),
        'mask_pos': torch.tensor(train_data['mask_pos'].get(list(range(len(train_data))))),
        'labels': torch.tensor(train_data['labels'].get(list(range(len(train_data))))),
    }
    dev_data = {
        'input_ids': torch.tensor(dev_data['input_ids'].get(list(range(len(dev_data))))),
        'attention_mask': torch.tensor(dev_data['attention_mask'].get(list(range(len(dev_data))))),
        'mask_pos': torch.tensor(dev_data['mask_pos'].get(list(range(len(dev_data))))),
        'labels': torch.tensor(dev_data['labels'].get(list(range(len(dev_data))))),
    }

model_forward_api = LMForwardAPI(
    model_name=model_name,
    task_name=task_name,
    loss_type=loss_type,
    lora_r = lora_r,
)

cma_opts = {
    'seed': seed,
    'popsize': popsize,
    'maxiter': budget // (popsize * model_forward_api.config.num_hidden_layers),
    'verbose': -1,
}
if bound > 0:
    cma_opts['bounds'] = [-1 * bound, 1 * bound]

es_list= [
    cma.CMAEvolutionStrategy(intrinsic_dim * [0], sigma, inopts=cma_opts)
    for i in range(model_forward_api.config.num_hidden_layers)
]
start_time = time.time()
size = int(intrinsic_dim/4)
for _ in range(budget // (int(popsize) * model_forward_api.config.num_hidden_layers)):
    for i, es in enumerate(es_list):
        solutions = es.ask()
        fitnesses = [model_forward_api.eval(x, i) for x in solutions]
        es.tell(solutions, fitnesses)
        results_QA = torch.tensor(es.result.xbest[:size],device=device).type(torch.float32)
        results_QB = torch.tensor(es.result.xbest[size:2*size],device=device).type(torch.float32)
        results_KKA = torch.tensor(es.result.xbest[2*size:3*size],device=device).type(torch.float32)
        results_KKB = torch.tensor(es.result.xbest[3*size:],device=device).type(torch.float32)
        model_forward_api.best_lora_A[i] = model_forward_api.lora_A_linear[i](
            results_QA).reshape(model_forward_api.config.hidden_size,-1)  # set best cv
        model_forward_api.best_lora_B[i] = model_forward_api.lora_B_linear[i](
            results_QB).reshape(-1,model_forward_api.config.hidden_size)   
        model_forward_api.best_lora_KA[i] = model_forward_api.lora_KA_linear[i](
            results_KKA).reshape(model_forward_api.config.hidden_size,-1)  # set best cv
        model_forward_api.best_lora_KB[i] = model_forward_api.lora_KB_linear[i](
            results_KKB).reshape(-1,model_forward_api.config.hidden_size)
end_time = time.time()
print('Done. Elapsed time: {} (mins)'.format((end_time - start_time) / 60))
print('Evaluate on test data...')
test_acc = model_forward_api.eval(test_data=test_data)
print('Test acc: {}'.format(round(test_acc, 4)))
# fitlog.finish()
