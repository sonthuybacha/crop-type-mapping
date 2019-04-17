import sys
sys.path.append("models")

import torch
from models.DualOutputRNN import DualOutputRNN
from models.ConvShapeletModel import ConvShapeletModel
from datasets.UCR_Dataset import UCRDataset
from datasets.BavarianCrops_Dataset import BavarianCropsDataset
from datasets.HDF5Dataset import HDF5Dataset
from datasets.Synthetic_Dataset import SyntheticDataset
from models.TransformerEncoder import TransformerEncoder
from models.transformer.Optim import ScheduledOptim
from datasets.ConcatDataset import ConcatDataset
import argparse
from argparse import Namespace
from utils.trainer import Trainer
import pandas as pd
import os
import numpy as np
from models.wavenet_model import WaveNetModel
from torch.utils.data.sampler import RandomSampler, SequentialSampler, BatchSampler, WeightedRandomSampler
from sampler.imbalanceddatasetsampler import ImbalancedDatasetSampler
from models.rnn import RNN
from utils.texparser import confusionmatrix2table, texconfmat
from utils.logger import Logger
from utils.visdomLogger import VisdomLogger
from models.transformer.Optim import ScheduledOptim
import torch.optim as optim

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-d','--datasets', type=str,default=None, nargs='+', help='UCR_Datasets Datasets to train on. Multiple values are allowed. '
                                                'Will also define name of the experiment. '
                                                'if not specified, will use all datasets of hyperparametercsv'
                                                '(requires --hyperparametercsv)')
    parser.add_argument(
        '-b', '--batchsize', type=int, default=32, help='Batch Size')
    parser.add_argument(
        '-m', '--model', type=str, default="rnn", help='Model variant either rnn or transformer')
    parser.add_argument(
        '-e', '--epochs', type=int, default=100, help='number of epochs')
    parser.add_argument(
        '-w', '--workers', type=int, default=4, help='number of CPU workers to load the next batch')
    parser.add_argument(
        '-l', '--learning_rate', type=float, default=0.1, help='learning rate')
    parser.add_argument(
        '--train_on', type=str, default="train", help="dataset partition to train. Choose from 'train', 'valid', "
                                                      "'trainvalid', 'eval' (default 'train')")
    parser.add_argument(
        '--test_on', type=str, default="valid",
        help="dataset partition to train. Choose from 'train', 'valid', 'trainvalid', 'eval' (default 'valid')")
    parser.add_argument(
        '--classmapping', type=str, default=None,
        help="classmapping for the bavarian crops dataset line-wise text file of format (0,0,415)")
    parser.add_argument(
        '--dropout', type=float, default=.2, help='dropout probability of the rnn layer')
    parser.add_argument(
        '--epsilon', type=float, default=.2, help='bias factor to add on P(t) as a regularization parameter')
    parser.add_argument(
        '-n', '--num_layers', type=int, default=1, help='number of stacked layers. will be interpreted as stacked '
                                                        'RNN layers for recurrent models and as number of convolutions'
                                                        'for convolutional models...')
    parser.add_argument(
        '-r', '--hidden_dims', type=int, default=32, help='number of hidden dimensions per layer stacked hidden dimensions')
    parser.add_argument(
        '--train-valid-split-seed', type=int, default=0,
        help='random seed for splitting of train and validation datasets. '
             'only applies for --train_on train and --test_on valid. see also --train-valid-ratio')
    parser.add_argument(
        '--train-valid-split-ratio', type=float, default=.75,
        help='ratio of splitting the train dataset in training and validation partitions. '
             'only applies for --train_on train and --test_on valid. see also --train-valid-split-seed')
    parser.add_argument('--overwrite', action='store_true',
                        help="Overwrite automatic snapshots if they exist")
    parser.add_argument(
        '-x', '--experiment', type=str, default="test", help='experiment prefix')
    parser.add_argument(
        '--store', type=str, default="/tmp", help='store run logger results')
    parser.add_argument(
        '--test_every_n_epochs', type=int, default=1, help='skip some test epochs for faster overall training')
    parser.add_argument(
        '--seed', type=int, default=None, help='seed for batching and weight initialization')
    parser.add_argument(
        '-i', '--show-n-samples', type=int, default=2, help='show n samples in visdom')
    args, _ = parser.parse_known_args()

    return args

def readHyperparameterCSV(args):
    print("reading "+args.hyperparametercsv)

    # get hyperparameters from the hyperparameter file for the current dataset...
    hparams = pd.read_csv(args.hyperparametercsv)

    # select only current dataset
    hparams = hparams.set_index("dataset").loc[args.dataset]

    args_dict = vars(args)

    for key,value in hparams.iteritems():

        # ignore empty columns
        if 'Unnamed' not in key:

            # only overwrite if key exists in parsed arguments
            if key in args_dict.keys():
                datatype_function = type(args_dict[key])
                value = datatype_function(value)

                args_dict[key] = value
                print("overwriting {key} with {value}".format(key=key,value=value))

    return Namespace(**args_dict)

def prepare_dataset(args):
    root = "/home/marc/data/BavarianCrops"

    dataset_holl = BavarianCropsDataset(root=root, region="HOLL_2018_MT_pilot", partition=args.train_on,
                                        classmapping=args.classmapping)
    dataset_krum = BavarianCropsDataset(root=root, region="KRUM_2018_MT_pilot", partition=args.train_on,
                                        classmapping=args.classmapping)
    dataset_nowa = BavarianCropsDataset(root=root, region="NOWA_2018_MT_pilot", partition=args.train_on,
                                        classmapping=args.classmapping)
    traindataset = ConcatDataset([dataset_holl, dataset_krum, dataset_nowa])

    traindataloader = torch.utils.data.DataLoader(dataset=traindataset, sampler=ImbalancedDatasetSampler(traindataset),
                                                  batch_size=args.batchsize, num_workers=args.workers)

    dataset_holl = BavarianCropsDataset(root=root, region="HOLL_2018_MT_pilot", partition=args.test_on,
                                        classmapping=args.classmapping)
    dataset_krum = BavarianCropsDataset(root=root, region="KRUM_2018_MT_pilot", partition=args.test_on,
                                        classmapping=args.classmapping)
    dataset_nowa = BavarianCropsDataset(root=root, region="NOWA_2018_MT_pilot", partition=args.test_on,
                                        classmapping=args.classmapping)
    testdataset = ConcatDataset([dataset_holl, dataset_krum, dataset_nowa])

    testdataloader = torch.utils.data.DataLoader(dataset=testdataset, sampler=SequentialSampler(testdataset),
                                                 batch_size=args.batchsize, num_workers=args.workers)

    return traindataloader, testdataloader

def train(args):

    traindataloader, testdataloader = prepare_dataset(args)

    #

    args.nclasses = traindataloader.dataset.nclasses
    classname = traindataloader.dataset.classname
    klassenname = traindataloader.dataset.klassenname
    args.seqlength = traindataloader.dataset.sequencelength
    args.input_dims = traindataloader.dataset.ndims

    model = getModel(args)



    store = os.path.join(args.store,args.experiment,args.dataset)

    logger = Logger(columns=["accuracy"], modes=["train", "test"], rootpath=store)

    visdomenv = "{}_{}".format(args.experiment, args.dataset)
    visdomlogger = VisdomLogger(env=visdomenv)

    optimizer = ScheduledOptim(
        optim.Adam(
            filter(lambda x: x.requires_grad, model.parameters()),
            betas=(0.9, 0.98), eps=1e-09),
        model.d_model, 4000)

    config = dict(
        epochs=args.epochs,
        learning_rate=args.learning_rate,
        show_n_samples=args.show_n_samples,
        store=store,
        visdomlogger=visdomlogger,
        overwrite=args.overwrite,
        test_every_n_epochs=args.test_every_n_epochs,
        logger=logger,
        optimizer=optimizer
    )


    trainer = Trainer(model,traindataloader,testdataloader,**config)
    logger = trainer.fit()

    # stores all stored values in the rootpath of the logger
    logger.save()

    confusionmatrix2table(store+"/npy/confusion_matrix_{epoch}.npy".format(epoch = args.epochs), classnames=klassenname)
    texconfmat("/tmp/test/BavarianCrops/npy/confusion_matrix_1.npy")
    #accuracy2table(store+"/npy/confusion_matrix_{epoch}.npy".format(epoch = args.epochs), classnames=klassenname)



    #stats = trainer.test_epoch(evaldataloader)

    pass

def getModel(args):

    if args.model == "rnn":
        model = RNN(input_dim=args.input_dims, nclasses=args.nclasses, hidden_dims=args.hidden_dims,
                              num_rnn_layers=args.num_layers, dropout=args.dropout, bidirectional=True)
    elif args.model == "transformer":

        hidden_dims = 256
        n_heads = 8
        n_layers = 6

        model = TransformerEncoder(in_channels=args.input_dims, len_max_seq=70,
            d_word_vec=hidden_dims, d_model=hidden_dims, d_inner=hidden_dims*4,
            n_layers=n_layers, n_head=n_heads, d_k=hidden_dims//n_heads, d_v=hidden_dims//n_heads,
            dropout=0.5, nclasses=args.nclasses)
        pass

    elif args.model == "WaveNet":

        model = WaveNetModel(
                 layers=5,
                 blocks=4,
                 dilation_channels=32,
                 residual_channels=32,
                 skip_channels=256,
                 end_channels=args.nclasses,
                 classes=args.nclasses,
                 output_length=1,
                 kernel_size=2,
                 dtype=torch.FloatTensor,
                 input_dims=args.input_dims,
                 bias=False)

    elif args.model == "Conv1D":
        model = ConvShapeletModel(num_layers=args.num_layers,
                                  hidden_dims=args.hidden_dims,
                                  ts_dim=args.input_dims,
                                  n_classes=args.nclasses,
                                  use_time_as_feature=True,
                                  seqlength=args.seqlength,
                                  scaleshapeletsize=args.shapelet_width_in_percent,
                                  drop_probability=args.dropout,
                                  shapelet_width_increment=args.shapelet_width_increment)
    else:
        raise ValueError("Invalid Model, Please insert either 'DualOutputRNN', or 'Conv1D'")

    if torch.cuda.is_available():
        model = model.cuda()

    return model

if __name__=="__main__":

    args = parse_args()
    for args.dataset in args.datasets:

        if os.path.exists(os.path.join(args.store,args.dataset)) and args.skip:
            print("path exists. skipping "+args.dataset)
            continue

        train(args)
