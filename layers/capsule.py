import math
import torch.nn as nn
from layers.models.cifar.resnet import BasicBlock, Bottleneck
from layers.cap_layer import CapLayer, CapLayer2, squash
import time
import torch


class CapsNet(nn.Module):
    """
    Capsule network.
    """
    def __init__(self, opts, num_classes=100):
        super(CapsNet, self).__init__()

        self.cap_model = opts.cap_model
        self.use_multiple = opts.use_multiple
        input_ch = 1 if opts.dataset == 'fmnist' else 3

        # Capsule part
        if self.cap_model != 'v_base':
            self.cap_N = opts.cap_N
            self.primary_cap_num = opts.primary_cap_num

        if self.cap_model == 'v_base':
            # baseline
            if hasattr(opts, 'depth'):
                depth = opts.depth
            else:
                depth = 20  # default value
            assert (depth - 2) % 6 == 0, 'depth should be 6n+2'
            self.inplanes = 16
            n = (depth - 2) / 6
            block = Bottleneck if depth >= 44 else BasicBlock

            if opts.dataset == 'tiny_imagenet':
                stride_num, fc_num, pool_stride, pool_kernel = 2, 64*4, 8, 8
            else:
                stride_num, fc_num, pool_stride, pool_kernel = 1, 64, 1, 8

            self.conv1 = nn.Conv2d(input_ch, 16, kernel_size=3, padding=1, bias=False, stride=stride_num)
            self.bn1 = nn.BatchNorm2d(16)
            self.relu = nn.ReLU(inplace=True)
            self.layer1 = self._make_layer(block, 16, n)
            self.layer2 = self._make_layer(block, 32, n, stride=2)
            self.layer3 = self._make_layer(block, 64, n, stride=2)
            self.avgpool = nn.AvgPool2d(pool_kernel, stride=pool_stride)          # TODO: which number
            self.fc = nn.Linear(fc_num, num_classes)

        elif self.cap_model == 'v0':
            # update Jan 17: original capsule idea in the paper
            # first conv
            self.tranfer_conv = nn.Conv2d(input_ch, opts.pre_ch_num, kernel_size=9, padding=1, stride=2)  # 256x13x13
            self.tranfer_bn = nn.InstanceNorm2d(opts.pre_ch_num, affine=True) \
                if opts.use_instanceBN else nn.BatchNorm2d(opts.pre_ch_num)
            self.tranfer_relu = nn.ReLU(True)
            # second conv
            factor = 8 if opts.w_version is 'v2' else 1
            send_to_cap_ch_num = self.primary_cap_num * factor
            self.tranfer_conv1 = nn.Conv2d(opts.pre_ch_num, send_to_cap_ch_num, kernel_size=3, stride=2)  # (say256)x6x6
            self.tranfer_bn1 = nn.InstanceNorm2d(send_to_cap_ch_num, affine=True) \
                if opts.use_instanceBN else nn.BatchNorm2d(send_to_cap_ch_num)
            self.tranfer_relu1 = nn.ReLU(True)
            # capsLayer
            self.cap_layer = CapLayer(opts, num_in_caps=self.primary_cap_num*6*6, num_out_caps=num_classes,
                                      out_dim=16, num_shared=self.primary_cap_num, in_dim=8)
        else:
            # different structures below
            ############ v1 ############
            self.buffer = nn.Sequential(*[
                nn.Conv2d(64, 64, kernel_size=3, padding=1),
                nn.ReLU(True)
            ])
            # then do squash in the forward pass
            # the new convolution capsule idea
            self.basic_cap = CapLayer2(opts, 64, 64, 8, 8, route_num=opts.route_num)
            self.cls_cap = CapLayer2(opts, 64, 64, 8, 10, as_final_output=True, route_num=opts.route_num)

            ############ v2,v3,v4,v5 ############
            # increase the spatial size x2 and channel number x1/2 (TOO SLOW)
            cap_dim = 128
            # self.buffer2 = nn.Sequential(*[
            #     nn.ConvTranspose2d(64, cap_dim, stride=2, kernel_size=1, output_padding=1),
            #     nn.ReLU(True)
            # ])
            self.buffer2 = nn.Sequential(*[
                nn.Conv2d(64, cap_dim, kernel_size=3, padding=1),
                nn.ReLU(True)
            ])
            self.cap_smaller_in_share = CapLayer2(
                opts, cap_dim, cap_dim, 8, 8, shared_size=4,
                route_num=opts.route_num)

            self.cap_smaller_in_out_share = CapLayer2(
                opts, cap_dim, cap_dim, 8, 8, shared_size=4,
                shared_group=2, route_num=opts.route_num)

            self.cls_smaller_in_share = CapLayer2(
                opts, cap_dim, cap_dim, 8, 10, shared_size=2,
                as_final_output=True, route_num=opts.route_num)

            # misc utilites and toys
            self.dropout = nn.Dropout2d(p=0.1)
            self.bummer = nn.Sequential(*[
                nn.BatchNorm2d(128),
                nn.ReLU(True)
            ])

        # init the network
        # TODO: merge with basic toolkit
        for m in self.modules():
            if isinstance(m, nn.Conv2d) or isinstance(m, nn.ConvTranspose2d):
                n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
                m.weight.data.normal_(0, math.sqrt(2. / n))
            elif isinstance(m, nn.BatchNorm2d) or isinstance(m, nn.InstanceNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()
            elif isinstance(m, nn.Linear):
                # print('linear layer!')
                m.weight.data.normal_(std=0.05)
                m.bias.data.zero_()
        # print('passed init')

    def forward(self, x, target=None, curr_iter=0, vis=None):
        stats = []
        multi_cap_stats = []
        # start = time.time()

        # TODO: merge the resnet part out of the capsule part
        # THE FOLLOWING ARE PARALLEL TO EACH OTHER
        if self.cap_model == 'v_base':
            x = self.conv1(x)
            x = self.bn1(x)
            x = self.relu(x)
            x = self.layer1(x)                  # 16 x 32 x 32
            x = self.layer2(x)                  # 32 x 16 x 16
            x = self.layer3(x)                  # 64(for depth=20) x 8 x 8
            x = self.avgpool(x)
            x = x.view(x.size(0), -1)
            x = self.fc(x)

        elif self.cap_model == 'v0':
            x = self.tranfer_conv(x)
            x = self.tranfer_bn(x)
            x = self.tranfer_relu(x)
            x = self.tranfer_conv1(x)
            x = self.tranfer_bn1(x)
            x = self.tranfer_relu1(x)
            # print('conv time: {:.4f}'.format(time.time() - start))
            start = time.time()
            x, stats = self.cap_layer(x, target, curr_iter, vis)
            # print('last cap total time: {:.4f}'.format(time.time() - start))

        elif self.cap_model == 'v1':
            x = self.buffer(x)
            x = self._do_squash(x)
            for i in range(self.cap_N):
                x, _ = self.basic_cap(x)
            x, stats = self.cls_cap(x)

        elif self.cap_model == 'v2':
            x = self.buffer2(x)
            x = self._do_squash(x)
            for i in range(self.cap_N):
                x, curr_stats = self.cap_smaller_in_share(x)
                multi_cap_stats.append(curr_stats)
            x, stats = self.cls_smaller_in_share(x)
            multi_cap_stats.append(stats)

        elif self.cap_model == 'v3':
            x = self.buffer2(x)
            x = self._do_squash(x)
            for i in range(self.cap_N):
                x, curr_stats = self.cap_smaller_in_out_share(x)
                multi_cap_stats.append(curr_stats)
            x, stats = self.cls_smaller_in_share(x)
            multi_cap_stats.append(stats)

        elif self.cap_model == 'v4_1':
            x = self.buffer2(x)
            x = self._do_squash(x)
            for i in range(self.cap_N):
                residual = x
                x, curr_stats = self.cap_smaller_in_share(x)
                multi_cap_stats.append(curr_stats)
                x += residual
            x, stats = self.cls_smaller_in_share(x)
            multi_cap_stats.append(stats)

        elif self.cap_model == 'v4_2':
            x = self.buffer2(x)
            x = self._do_squash(x)
            for i in range(self.cap_N):
                residual = x
                x = self.cap_smaller_in_share(x)
                x += residual
                x = self._do_squash(x)
            x = self.cls_smaller_in_share(x)

        elif self.cap_model == 'v4_3':
            x = self.buffer2(x)
            x = self._do_squash(x)
            for i in range(self.cap_N):
                residual = x
                x = self.cap_smaller_in_share(x)
                x = self.dropout(x)
                x += residual
            x = self.cls_smaller_in_share(x)

        elif self.cap_model == 'v4_4':
            x = self.buffer2(x)
            x = self._do_squash(x)
            for i in range(self.cap_N):
                residual = x
                x = self.cap_smaller_in_share(x)
                x = self.dropout(x)
                x += residual
                x = self._do_squash(x)
            x = self.cls_smaller_in_share(x)

        elif self.cap_model == 'v4_5':
            x = self.buffer2(x)
            x = self._do_squash(x)
            for i in range(self.cap_N):
                residual = x
                residual = self.dropout(residual)
                x = self.cap_smaller_in_share(x)
                x += residual
            x = self.cls_smaller_in_share(x)

        elif self.cap_model == 'v4_6':
            x = self.buffer2(x)
            x = self._do_squash(x)
            for i in range(self.cap_N):
                residual = x
                residual = self.dropout(residual)
                x = self.cap_smaller_in_share(x)
                x += residual
                x = self._do_squash(x)
            x = self.cls_smaller_in_share(x)

        elif self.cap_model == 'v4_7':
            x = self.buffer2(x)
            x = self._do_squash(x)
            for i in range(self.cap_N):
                residual = x
                residual = self.dropout(residual)
                x = self.cap_smaller_in_share(x)
                x = self.dropout(x)
                x += residual
            x = self.cls_smaller_in_share(x)

        elif self.cap_model == 'v4_8':
            x = self.buffer2(x)
            x = self._do_squash(x)
            for i in range(self.cap_N):
                residual = x
                residual = self.dropout(residual)
                x = self.cap_smaller_in_share(x)
                x = self.dropout(x)
                x += residual
                x = self._do_squash(x)
            x = self.cls_smaller_in_share(x)
        elif self.cap_model == 'v5_1':
            x = self.buffer2(x)
            x = self._do_squash(x)
            for i in range(self.cap_N):
                residual, x1, x2 = x, x, x
                x1, curr_stats = self.cap_smaller_in_share(x1)
                multi_cap_stats.append(curr_stats)
                x2, curr_stats = self.cap_smaller_in_out_share(x2)
                multi_cap_stats.append(curr_stats)
                x = residual + x1 + x2
            x, stats = self.cls_smaller_in_share(x)
            multi_cap_stats.append(stats)

        elif self.cap_model == 'v5_2':
            x = self.buffer2(x)
            x = self._do_squash(x)
            for i in range(self.cap_N):
                residual, x1, x2 = x, x, x
                x1, _ = self.cap_smaller_in_share(x1)
                x2, _ = self.cap_smaller_in_out_share(x2)
                x = residual + x1 + x2
                x = self._do_squash(x)
            x, stats = self.cls_smaller_in_share(x)
        else:
            raise NameError('Unknown structure or capsule model type.')

        if self.use_multiple:
            stats = self._sort_up_multi_stats(multi_cap_stats)
        return x, stats

    def _sort_up_multi_stats(self, multi_cap_stats):
        stats = [multi_cap_stats[0][j] for j in range(len(multi_cap_stats[0]))]
        for i in range(1, len(multi_cap_stats)):
            for j in range(len(multi_cap_stats[0])):
                stats[j] = torch.cat((stats[j], multi_cap_stats[i][j]), dim=0)
        return stats

    def _do_squash(self, x):
        # do squash along the channel dimension
        spatial_size = x.size(2)
        input_channel = x.size(1)
        x = x.resize(x.size(0), x.size(1), int(spatial_size**2)).permute(0, 2, 1)
        x = squash(x)
        x = x.permute(0, 2, 1).resize(x.size(0), input_channel, spatial_size, spatial_size)
        return x

    def _make_layer(self, block, planes, blocks, stride=1):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                nn.Conv2d(self.inplanes, planes * block.expansion,
                          kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample))
        self.inplanes = planes * block.expansion
        for i in range(1, int(blocks)):
            layers.append(block(self.inplanes, planes))

        return nn.Sequential(*layers)
