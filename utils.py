import torch
import torch.nn as nn
import math

class Metrics:
    def __init__(self, y_true, y_pred):
        self.mse = nn.MSELoss()
        self.rmse = torch.sqrt(self.mse(y_true, y_pred))
        self.mape = 100 * abs(torch.subtract(y_true, y_pred)) / y_true
