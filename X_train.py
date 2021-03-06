import torch
import torch.nn as nn
from torch.utils.tensorboard import SummaryWriter
import z3
from Doping.pytorchtreelstm.treelstm import TreeLSTM, calculate_evaluation_orders
from Doping.utils.X_Dataset import DataObj
from Doping.settings import MODEL_PATH, new_model_path
from X_model import Model
import json
import os
from termcolor import colored
import argparse
import random
import logging

import matplotlib.pyplot as plt
import Doping.utils.utils as Du
from Doping.X_eval import evaluate

if __name__ == '__main__':
    parser = Du.parser_from_template()
    parser.add_argument("--json_config_file", "-JI", required=True, help="Path to the json config")
    parser.add_argument("-l", "--log", dest="logLevel", choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], default='CRITICAL', help="Set the logging level")
    args = parser.parse_args()
    log = logging.getLogger(__name__)
    log.setLevel(getattr(logging, args.logLevel))
    #load the config file
    with open(args.json_config_file, "r") as f:
        configs = json.load(f)

    #overwrite configs with parser value if user provide input
    for key,value in vars(args).items():
        if value is None or key not in configs:
            continue
        else:
            print("Setting {} in configs to {}".format(key, value))
            configs[key][0] = value

    exp_name = Du.get_exp_name(configs)
    SWRITER = SummaryWriter(comment = exp_name)
    #NOTE: batch_size should not be a divisor of the number of dps in train set or test set (batch_size = 32 while train has 4000 is not good)
    print("Configs:\n", json.dumps(configs, indent=2))
    dataObjs = []
    vocabs = []
    device = torch.device(configs['device'][0])
    for exp_folder in configs["input_folders"]:
        dataObj = DataObj(exp_folder,
                          device = device,
                          max_size = configs["max_size"][0],
                          shuffle = configs["shuffle"][0],
                          train_size = 1,
                          threshold = configs["threshold"][0],
                          negative = configs["train_negative_model"][0]
                          )
        vocab = dataObj.vocab
        dataObjs.append(dataObj)
        vocabs.append(vocab)

    model = Model(vocabs[0]['size'],
                  vocabs[0]['sort_size'],
                  emb_dim = configs['emb_dim'][0],
                  const_emb_dim = vocabs[0]["const_emb_size"],
                  tree_dim = configs['tree_dim'][0],
                  use_const_emb = configs["use_const_emb"][0],
                  use_dot_product = configs["use_dot_product"][0],
                  dropout_rate = configs["dropout_rate"][0],
                  device = device).train()

    no_of_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print("no_of_params:", no_of_params)
    if configs["checkpoint"][0]!="":
        print("Training start from this checkpoint:\n{}".format(configs["checkpoint"][0]))
        checkpoint = torch.load(configs["checkpoint"][0])
        model.load_state_dict(checkpoint["model_state_dict"])

    loss_function = torch.nn.CrossEntropyLoss().to(device)
    optimizer = torch.optim.Adam(model.parameters())

    metadata = {"dataset": dataObj.metadata(), "model": model.metadata(), "configs": configs, "no_of_params": no_of_params}
    SWRITER.add_text('metadata', json.dumps(metadata, indent = 2)  )
    # examples_idx = random.sample(list(range(len(dataObj.test_dps))), 20)
    for n in range(configs["epoch"][0]):
        for dataObj in dataObjs:
            last_batch = False
            total_loss = 0
            while not last_batch:
                optimizer.zero_grad()
                loss = 0
                train, last_batch = dataObj.next_batch(dataObj.train_P ,
                                                    batch_size = configs["train_batch_size"][0],
                                                    negative_sampling_rate = configs["negative_sampling_rate"][0])
                # print(last_batch)
                # print("Training with %d datapoints"%train["size"])
                if train is not None:
                    output = model(
                        train["L_a_batch"],
                        train["L_b_batch"]
                    )[0]
                    loss = loss_function(output, train["label_batch"].to(device))
                    total_loss += loss

                    loss.backward()
                    optimizer.step()

            if n%configs["eval_epoch"][0]==0:
                print("Result using data from: {}".format(str(dataObj)))
                # print(output.shape)
                train_res = evaluate(model, dataObj, dataObj.train_P, configs["train_batch_size"][0])
                # print("example_ids:", examples_idx)
                # test_res = evaluate(model, dataObj.test_P)
                for name, weight in model.named_parameters():
                    SWRITER.add_histogram(name,weight, n)
                    # SWRITER.add_histogram(f'{name}.grad',weight.grad, n)

                    if len(list(weight.data.size()))==2:
                        image = weight.data.cpu().detach().numpy()
                        log.debug(image)
                        image -= image.min()
                        image /= image.max()
                        log.debug(weight.data.size())
                        log.debug(image)
                        SWRITER.add_image("weight"+str(name), image, global_step = n,  dataformats='HW')


                SWRITER.add_scalar('Loss/train', total_loss, n)
                SWRITER.add_scalar('Accuracy/train', train_res["acc"], n)
                # SWRITER.add_scalar('Accuracy/test', test_res["acc"], n)
                SWRITER.add_scalar('F1/train', train_res["f1"], n)
                # SWRITER.add_scalar('F1/test', test_res["f1"], n)
                print(f'Iteration {n+1} Loss: {loss}')
                #check that embedding is being trained
                print(model.emb(torch.LongTensor([5]).to(device = device ) ) )

        if n%configs["save_epoch"][0]==0 or n==configs["epoch"][0] - 1:
            model_path = new_model_path(basename = exp_name, epoch = n)
            print("Saving to ", model_path)
            torch.save({
                'epoch': n,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'loss': loss,
                'dataset': dataObj.metadata(),
                'metadata': model.metadata(),
                'configs': configs,
                'no_of_params': no_of_params
            }, model_path)
