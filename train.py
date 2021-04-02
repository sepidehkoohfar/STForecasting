import pickle
from preprocess import Scaler
from utils import Metrics
from attn import Attn
from torch.optim import Adam
import torch.nn as nn
import numpy as np
import torch
from utils import inverse_transform
from baselines import RNN, CNN
import argparse
import json
import os
import pytorch_warmup as warmup
from clearml import Task, Logger


def batching(batch_size, x_en, x_de, y_t):

    batch_n = int(x_en.shape[0] / batch_size)
    start = x_en.shape[0] % batch_n
    X_en = torch.zeros(batch_n, batch_size, x_en.shape[1], x_en.shape[2])
    X_de = torch.zeros(batch_n, batch_size, x_de.shape[1], x_de.shape[2])
    Y_t = torch.zeros(batch_n, batch_size, y_t.shape[1], y_t.shape[2])

    for i in range(batch_n):
        X_en[i, :, :, :] = x_en[start:start+batch_size, :, :]
        X_de[i, :, :, :] = x_de[start:start+batch_size, :, :]
        Y_t[i, :, :, :] = y_t[start:start+batch_size, :, :]
        start += batch_size

    return X_en, X_de, Y_t


erros = dict()

if torch.cuda.is_available():
    device = torch.device("cuda:0")
    print("Running on GPU")
else:
    device = torch.device("cpu")
    print("running on CPU")


def train(args, model, train_en, train_de, train_y, test_en, test_de, test_y):

    optimizer = Adam(model.parameters(), lr=args.lr)
    criterion = nn.MSELoss()
    num_steps = len(train_en) * args.n_epochs
    lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=num_steps)
    warmup_scheduler = warmup.UntunedLinearWarmup(optimizer)

    for epoch in range(args.n_epochs):

        model.train()
        total_loss = 0
        for batch_id in range(train_en.shape[0]):
            output = model(train_en[batch_id], train_de[batch_id], training=True)
            loss = criterion(output, train_y[batch_id])
            total_loss += loss.item()
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            lr_scheduler.step()
            warmup_scheduler.dampen()
            total_loss += loss.item()

        Logger.current_logger().report_scalar(title="evaluate", series="loss", iteration=epoch, value=total_loss)
        if epoch % 20 == 0:
            print("Train epoch: {}, loss: {:.4f}".format(epoch, total_loss))

        model.eval()
        test_loss = 0
        for j in range(test_en.shape[0]):
            output = model(test_en[j].to(device), test_de[j].to(device), training=True)
            loss = criterion(test_y[j].to(device), output)
            test_loss += loss.item()

        test_loss = test_loss / test_en.shape[0]
        Logger.current_logger().report_scalar(title="evaluate", series="loss", iteration=epoch, value=test_loss)
        if epoch % 20 == 0:

            print("Average loss: {:.3f}".format(test_loss))


def main():

    task = Task.init(project_name='watershed', task_name='watershed training')

    parser = argparse.ArgumentParser(description="preprocess argument parser")
    parser.add_argument("--seq_len_pred", type=int, default=64)
    parser.add_argument("--batch_size", type=int, default=16)
    parser.add_argument("--cutoff", type=int, default=16)
    parser.add_argument("--d_model", type=int, default=32)
    parser.add_argument("--dff", type=int, default=64)
    parser.add_argument("--n_heads", type=int, default=4)
    parser.add_argument("--n_layers", type=int, default=1)
    parser.add_argument("--kernel", type=int, default=1)
    parser.add_argument("--out_channel", type=int, default=32)
    parser.add_argument("--dr", type=float, default=0.5)
    parser.add_argument("--lr", type=float, default=0.0001)
    parser.add_argument("--n_epochs", type=int, default=300)
    parser.add_argument("--run_num", type=int, default=1)
    parser.add_argument("--pos_enc", type=str, default='sincos')
    parser.add_argument("--attn_type", type=str, default='attn')
    parser.add_argument("--name", type=str, default='attn')
    parser.add_argument("--site", type=str, default="WHB")
    parser.add_argument("--server", type=str, default="c01")
    args = parser.parse_args()
    configs = {'n_heads': 4,
               'n_layers': 1,
               'dr': 0.5,
               'lr': 0.0001,
               'seq_len_pred': args.seq_len_pred,
               'batch_size': args.batch_size,
               'cutoff': args.cutoff,
               'd_model': args.d_model,
               'dff': args.dff,
               'kernel': args.kernel,
               'out_channel': args.out_channel,
               'n_epochs': args.n_epochs,
               'run_num': args.run_num,
               'pos_enc': args.pos_enc,
               'attn_type': args.attn_type,
               'name': args.name,
               'site': args.site,
               'server': args.server}
    configs = task.connect(configs)

    path = "models_{}_{}".format(configs.get('site'), configs.get("seq_len_pred"))
    if not os.path.exists(path):
        os.makedirs(path)

    inputs = pickle.load(open("inputs.p", "rb"))
    outputs = pickle.load(open("outputs.p", "rb"))

    max_len = min(len(inputs), 1024)
    inputs = inputs[-max_len:, :, :]
    outputs = outputs[-max_len:, :]
    seq_len = int(inputs.shape[1] / 2)

    data_en, data_de, data_y = batching(configs.get("batch_size"), inputs[:, :-seq_len, :],
                                  inputs[:, -seq_len:, :], outputs[:, :, :])

    test_en, test_de, test_y = data_en[-4:, :, :, :], data_de[-4:, :, :, :], data_y[-4:, :, :, :]
    train_en, train_de, train_y = data_en[:-4, :, :, :], data_de[:-4, :, :, :], data_y[:-4, :, :, :]

    d_k = int(configs.get("d_model") / configs.get("n_heads"))
    model = Attn(src_input_size=train_en.shape[3],
                 tgt_input_size=train_y.shape[3],
                 d_model=configs.get("d_model"),
                 d_ff=configs.get("dff"),
                 d_k=d_k, d_v=d_k, n_heads=configs.get("n_heads"),
                 n_layers=configs.get("n_layers"), src_pad_index=0,
                 tgt_pad_index=0, device=device,
                 pe=configs.get("pos_enc"), attn_type=configs.get("attn_type"),
                 seq_len=seq_len, seq_len_pred=configs.get("seq_len_pred"),
                 cutoff=configs.get("cutoff"), dr=args.get("dr")).to(device)

    train(args, model, train_en.to(device), train_de.to(device),
          train_y.to(device), test_en.to(device), test_de.to(device), test_y.to(device))

    torch.save(model.state_dict(), os.path.join(path, configs.get("name")))

    print('Task ID number is: {}'.format(task.id))


if __name__ == '__main__':
    main()