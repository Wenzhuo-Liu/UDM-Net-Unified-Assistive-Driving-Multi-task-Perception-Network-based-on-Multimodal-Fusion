from email.policy import strict
from stat import S_ENFMT
from numpy import outer
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from torch.autograd import Variable

from .gconv import ConvTemporalGraphical, HD_Gconv
from .graph import Graph
class ST_GCN_18(nn.Module):
    r"""Spatial temporal graph convolutional networks.

    Args:
        in_channels (int): Number of channels in the input data
        num_class (int): Number of classes for the classification task
        graph_cfg (dict): The arguments for building the graph
        edge_importance_weighting (bool): If ``True``, adds a learnable
            importance weighting to the edges of the graph
        **kwargs (optional): Other parameters for graph convolution units

    Shape:
        - Input: :math:`(N, in_channels, T_{in}, V_{in}, M_{in})`
        - Output: :math:`(N, num_class)` where
            :math:`N` is a batch size,
            :math:`T_{in}` is a length of input sequence,
            :math:`V_{in}` is the number of graph nodes,
            :math:`M_{in}` is the number of instance in a frame.
    """
    def __init__(self, in_channels=3, kernel_size=[9,2], num_class=4, A=Graph().A,edge_importance_weighting=True, data_bn = True, **kwargs):
        super().__init__()

        # self.data_shape = data_shape
        self.num_class = num_class
        # A=A.toarray()
        # self.A = A
        # kwargs = {
        #     'data_shape': self.data_shape,
        #     'num_class': self.num_class,
        #     'A': torch.Tensor(self.A),
        # }

        # set kernel size
        t_kernel_size = kernel_size[0]
        s_kernel_size = kernel_size[1] + 1
        # kernel_size = (temporal_kernel_size, spatial_kernel_size)

        # init adjacency matrix
        A = torch.tensor(A[:s_kernel_size,:,:], dtype=torch.float32, requires_grad=False)
        # self.A_rnl = torch.tensor(A_rnl[:,s_kernel_size, :, :], dtype=torch.float32, requires_grad=False)
        # self.register_buffer('A', A[:s_kernel_size,:,:])

        # build networks
        self.data_bn = nn.BatchNorm1d(in_channels * A.size(1)) if data_bn else lambda x:x
        kwargs0 = {k:v for k,v in kwargs.items() if k!= 'dropout'}
        self.st_gcn_networks = nn.ModuleList((
            st_gcn_block_hd(in_channels, 64, s_kernel_size, t_kernel_size, A, 1, residual=False, **kwargs0),
            st_gcn_block_hd(64, 64, s_kernel_size, t_kernel_size, A, 1, **kwargs),
            st_gcn_block_hd(64, 64, s_kernel_size, t_kernel_size, A, 1, **kwargs),
            st_gcn_block_hd(64, 64, s_kernel_size, t_kernel_size, A, 1, **kwargs),
            st_gcn_block_hd(64, 128, s_kernel_size, t_kernel_size, A, 2, **kwargs),
            st_gcn_block_hd(128, 128, s_kernel_size, t_kernel_size, A, 1, **kwargs),
            st_gcn_block_hd(128, 128, s_kernel_size, t_kernel_size, A, 1, **kwargs),
            st_gcn_block_hd(128, 256, s_kernel_size, t_kernel_size, A, 2, **kwargs),
            st_gcn_block_hd(256, 256, s_kernel_size, t_kernel_size, A, 1, **kwargs),
            # st_gcn_block_hd(256, 256, s_kernel_size, t_kernel_size, A, 1, **kwargs),
            st_gcn_block_hd(256, 512, s_kernel_size, t_kernel_size, A, 1, **kwargs)
        ))

        # initialize parameters for edge importance weighting
        if edge_importance_weighting:
            self.edge_importance = nn.ParameterList([
                nn.Parameter(torch.ones(A.size()))
                for _ in self.st_gcn_networks
            ])
        else:
            self.edge_importance = [1] * len(self.st_gcn_networks)
        
        # fcn for prediction
        self.fcn = nn.Conv2d(256, num_class, kernel_size=1)
        
    def forward(self, x):

        # data normalization
        x = torch.squeeze(x, dim=1)#维度压缩，把1去掉
        N, C, T, V, M = x.size()
        x = x.permute(0, 4, 3, 1, 2).contiguous() # N, M, V, C, T
        x = x.view(N*M, V*C, T)
        x = self.data_bn(x)
        x = x.view(N, M, V, C, T)
        x = x.permute(0, 1, 3, 4, 2).contiguous()
        x = x.view(N*M, C, T, V)

        # forward
        for gcn, edge in zip(self.st_gcn_networks, self.edge_importance):
            x= gcn(x, edge)
        # for gcn in (self.st_gcn_networks):
        #     x = gcn(x)
        
        _, c, t, v = x.size()
        # x = x.view(N, C, T, V)
        feature = x.view(N, M, c, t, v).permute(0, 2, 3, 4, 1)


        gap = nn.AdaptiveAvgPool2d((1, 1))
        x = gap(x.view(N, c, t*v, 1)).view(N, c)
        # # global pooling#这里的池化变一下--[N,512,1,1]
        # x = F.avg_pool2d(x, x.size()[2:])
        # x = x.view(N, M, -1, 1, 1).mean(dim=1)

        # prediction
        # x = self.fcn(x)
        # x = x.view(x.size(0), -1)#展平为一维
        
        return x #, feature

    def extract_feature(self, x):
        
        # data normalization
        N, C, T, V, M = x.size()
        x = x.permute(0, 4, 3, 1, 2).contiguous()
        x = x.view(N * M, V * C, T)
        x = self.data_bn(x)
        x = x.view(N, M, V, C, T)
        x = x.permute(0, 1, 3, 4, 2).contiguous()
        x = x.view(N * M, C, T, V)

        # forwad
        for gcn, importance in zip(self.st_gcn_networks, self.edge_importance):
            x= gcn(x, self.A * importance)

        _, c, t, v = x.size()
        feature = x.view(N, M, c, t, v).permute(0, 2, 3, 4, 1)

        # prediction
        x = self.fcn(x)
        output = x.view(N, M, -1, t, v).permute(0, 2, 3, 4, 1)

        return output, feature


class st_gcn_block(nn.Module):
    r"""Applies a spatial temporal graph convolution over an input graph sequence.

    Args:
        in_channels (int): Number of channels in the input sequence data
        out_channels (int): Number of channels produced by the convolution
        kernel_size (tuple): Size of the temporal convolving kernel and graph convolving kernel
        stride (int, optional): Stride of the temporal convolution. Default: 1
        dropout (int, optional): Dropout rate of the final output. Default: 0
        residual (bool, optional): If ``True``, applies a residual mechanism. Default: ``True``

    Shape:
        - Input[0]: Input graph sequence in :math:`(N, in_channels, T_{in}, V)` format
        - Input[1]: Input graph adjacency matrix in :math:`(K, V, V)` format
        - Output[0]: Outpu graph sequence in :math:`(N, out_channels, T_{out}, V)` format
        - Output[1]: Graph adjacency matrix for output data in :math:`(K, V, V)` format

        where
            :math:`N` is a batch size,
            :math:`K` is the spatial kernel size, as :math:`K == kernel_size[1]`,
            :math:`T_{in}/T_{out}` is a length of input/output sequence,
            :math:`V` is the number of graph nodes.

    """
    def __init__(self, in_channels, out_channels, spatial_kernel_size, temporal_kernel_size, A, stride=1, dropout=0, residual=True, adaptive=True, **kwargs):
        super().__init__()

        assert temporal_kernel_size%2 == 1
        padding = ((temporal_kernel_size - 1)//2, 0)

        # inter_channels = out_channels // coff_embedding
        # self.inter_c = inter_channels

        if adaptive:
            self.A = nn.Parameter(A, requires_grad=True)
            # Graph1=Graph()
            # self.A = Graph1._get_adjacency()
        else:
            self.register_buffer('A',A)

        self.alpha = nn.Parameter(torch.zeros(1))
        self.gcn = ConvTemporalGraphical(in_channels, out_channels, spatial_kernel_size)
        # self.ctr = CTRGC(in_channels,out_channels)
        self.tcn = nn.Sequential(
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels,out_channels,(temporal_kernel_size,1),(stride,1),padding),
            nn.BatchNorm2d(out_channels),
            nn.Dropout(dropout, inplace=True)
        )

        if not residual:
            self.residual = lambda x:0
        elif (in_channels == out_channels) and (stride == 1):
            self.residual = lambda x:x
        else:
            self.residual = nn.Sequential(
                nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=(stride,1)),
                nn.BatchNorm2d(out_channels)
            )
        
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x, edge):

        res = self.residual(x)
        x, _ = self.gcn(x, self.A*edge)
        # print(x.is_cuda)
        # device = torch.device("cpu" if torch.cuda.is_available() else "cuda:0")
        # x = x.to(device)
        # x2 = self.ctr(x)
        # x = x2 + x1
        x = self.tcn(x) + res
        x = self.relu(x)

        return x


class st_gcn_block_hd(nn.Module):
    r"""Applies a spatial temporal graph convolution over an input graph sequence.

    Args:
        in_channels (int): Number of channels in the input sequence data
        out_channels (int): Number of channels produced by the convolution
        kernel_size (tuple): Size of the temporal convolving kernel and graph convolving kernel
        stride (int, optional): Stride of the temporal convolution. Default: 1
        dropout (int, optional): Dropout rate of the final output. Default: 0
        residual (bool, optional): If ``True``, applies a residual mechanism. Default: ``True``

    Shape:
        - Input[0]: Input graph sequence in :math:`(N, in_channels, T_{in}, V)` format
        - Input[1]: Input graph adjacency matrix in :math:`(K, V, V)` format
        - Output[0]: Outpu graph sequence in :math:`(N, out_channels, T_{out}, V)` format
        - Output[1]: Graph adjacency matrix for output data in :math:`(K, V, V)` format

        where
            :math:`N` is a batch size,
            :math:`K` is the spatial kernel size, as :math:`K == kernel_size[1]`,
            :math:`T_{in}/T_{out}` is a length of input/output sequence,
            :math:`V` is the number of graph nodes.

    """

    def __init__(self, in_channels, out_channels, spatial_kernel_size, temporal_kernel_size, A, stride=1, dropout=0,
                 residual=True, adaptive=True, att=False, CoM=21,**kwargs):
        super().__init__()

        assert temporal_kernel_size % 2 == 1
        padding = ((temporal_kernel_size - 1) // 2, 0)

        # inter_channels = out_channels // coff_embedding
        # self.inter_c = inter_channels

        if adaptive:
            self.A = nn.Parameter(A, requires_grad=True)
            # Graph1=Graph()
            # self.A = Graph1._get_adjacency()
        else:
            self.register_buffer('A', A)

        self.alpha = nn.Parameter(torch.zeros(1))
        self.gcn = HD_Gconv(in_channels, out_channels, A, adaptive=adaptive, att=att, CoM=CoM) #(in_channels, out_channels, spatial_kernel_size)
        # self.ctr = CTRGC(in_channels,out_channels)
        self.tcn = nn.Sequential(
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, (temporal_kernel_size, 1), (stride, 1), padding),
            nn.BatchNorm2d(out_channels),
            nn.Dropout(dropout, inplace=True)
        )

        if not residual:
            self.residual = lambda x: 0
        elif (in_channels == out_channels) and (stride == 1):
            self.residual = lambda x: x
        else:
            self.residual = nn.Sequential(
                nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=(stride, 1)),
                nn.BatchNorm2d(out_channels)
            )

        self.relu = nn.ReLU(inplace=True)

    def forward(self, x,edge):

        res = self.residual(x)
        x= self.gcn(x, self.A * edge)
        # print(x.is_cuda)
        # device = torch.device("cpu" if torch.cuda.is_available() else "cuda:0")
        # x = x.to(device)
        # x2 = self.ctr(x)
        # x = x2 + x1
        x = self.tcn(x) + res
        x = self.relu(x)

        return x