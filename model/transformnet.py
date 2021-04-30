import torch
from torch import nn
import torch.nn.functional as F

import model.resnet as models
from model.transformer.transformer import Transformer
from model.transformer.position_encoding import PositionEmbeddingSine


class PSPModule(nn.Module):
    # (1, 2, 3, 6)
    def __init__(self, sizes=(1, 3, 6, 8), dimension=2):
        super(PSPModule, self).__init__()
        self.stages = nn.ModuleList([self._make_stage(size, dimension) for size in sizes])
        self._init_weight()

    def _make_stage(self, size, dimension=2):
        if dimension == 1:
            prior = nn.AdaptiveAvgPool1d(output_size=size)
        elif dimension == 2:
            prior = nn.AdaptiveAvgPool2d(output_size=(size, size))
        elif dimension == 3:
            prior = nn.AdaptiveAvgPool3d(output_size=(size, size, size))
        return prior

    def forward(self, feats):
        n, c, _, _ = feats.size()
        priors = [stage(feats).view(n, c, -1) for stage in self.stages]
        center = torch.cat(priors, -1)
        return center

    def _init_weight(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                torch.nn.init.kaiming_normal_(m.weight)
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()


class TransformNet(nn.Module):
    def __init__(self, layers=50, bins=(1, 3, 6, 8), dropout=0.2, classes=2, zoom_factor=8,
                 use_aspp=True, output_stride=8, criterion=nn.CrossEntropyLoss(ignore_index=255),
                 hidden_dim=512, BatchNorm=nn.BatchNorm2d, pretrained=True, ImLength=110):
        super(TransformNet, self).__init__()
        assert layers in [50, 101, 152]
        assert 2048 % len(bins) == 0
        assert classes > 1
        assert zoom_factor in [1, 2, 4, 8]
        self.zoom_factor = zoom_factor
        self.use_aspp = use_aspp
        self.criterion = criterion
        self.os = output_stride
        self.bins = bins
        models.BatchNorm = BatchNorm

        if layers == 50:
            resnet = models.resnet50(output_stride, BatchNorm, pretrained=pretrained)
        elif layers == 101:
            resnet = models.resnet101(output_stride, BatchNorm, pretrained=pretrained)
        else:
            resnet = models.resnet152(output_stride, BatchNorm, pretrained=pretrained)
        self.layer0 = nn.Sequential(resnet.conv1, resnet.bn1, resnet.relu, resnet.conv2, resnet.bn2, resnet.relu,
                                    resnet.conv3, resnet.bn3, resnet.relu, resnet.maxpool)
        self.layer1, self.layer2, self.layer3, self.layer4 = resnet.layer1, resnet.layer2, resnet.layer3, resnet.layer4

        self.feat_proj = nn.Conv2d(resnet.num_channels[-1], hidden_dim, kernel_size=1)

        self.ppm = PSPModule(sizes=(1, 3, 6, 8), dimension=2)

        self.pos_enc = PositionEmbeddingSine(hidden_dim // 2, normalize=True)
        self.transformer = Transformer(d_model=hidden_dim, nhead=8, num_encoder_layers=0,
                                       num_decoder_layers=6, dim_feedforward=2048, dropout=0.1,
                                       activation="relu", normalize_before=False,
                                       return_intermediate_dec=False)

        self.query_embed = nn.Embedding(ImLength, hidden_dim)

        self.cls = nn.Sequential(
            nn.Conv2d(hidden_dim*(1+len(bins)), 512, kernel_size=3, padding=1, bias=False),
            BatchNorm(512),
            nn.ReLU(inplace=True),
            nn.Dropout2d(p=dropout),
            nn.Conv2d(512, classes, kernel_size=1)
        )
        if self.training:
            self.aux = nn.Sequential(
                nn.Conv2d(1024, 256, kernel_size=3, padding=1, bias=False),
                BatchNorm(256),
                nn.ReLU(inplace=True),
                nn.Dropout2d(p=dropout),
                nn.Conv2d(256, classes, kernel_size=1)
            )
            self.tr_dec_aux1 = nn.Sequential(
                nn.Conv2d(hidden_dim*(1+len(bins)), 512, kernel_size=3, padding=1, bias=False),
                BatchNorm(512),
                nn.ReLU(inplace=True),
                nn.Dropout2d(p=dropout),
                nn.Conv2d(512, classes, kernel_size=1)
            )

    def forward(self, x, y=None):
        x_size = x.size()

        h = int((x_size[2] - 1) / 8 * self.zoom_factor + 1)
        w = int((x_size[3] - 1) / 8 * self.zoom_factor + 1)

        x = self.layer0(x)
        f1 = self.layer1(x)
        f2 = self.layer2(f1)
        f3 = self.layer3(f2)
        f3_aux = f3
        f4 = self.layer4(f3)

        proj_f4 = self.feat_proj(f4)
        spp = self.ppm(proj_f4)

        proj_f4_half = F.interpolate(proj_f4, scale_factor=0.3, mode='bilinear', align_corners=True)
        pos = self.pos_enc(proj_f4_half)
        mask = torch.zeros(torch.max(proj_f4_half, dim=1)[0].size()).type(torch.BoolTensor).cuda()

        tr_output, tr_aux1 = self.transformer(src=proj_f4_half, mask=mask, tgt=spp, query_embed=self.query_embed.weight,
                                             pos_embed=pos)

        bsf, cf, hf, wf = proj_f4.shape

        psp_idx = 0
        psp_cat = proj_f4
        psp_cat_aux1 = proj_f4
        for i in self.bins:
            square = i**2
            pooled_output = tr_output[:,:,psp_idx:psp_idx+square].view(bsf, cf, i, i)
            pooled_resized_output = F.interpolate(pooled_output, size=proj_f4.size()[-2:], mode='bilinear', align_corners=True)
            psp_cat = torch.cat([psp_cat, pooled_resized_output], dim=1)

            if self.training:
                pooled_aux1_output = tr_aux1[:,:,psp_idx:psp_idx+square].view(bsf, cf, i, i)
                pooled_resized_aux1_output = F.interpolate(pooled_aux1_output, size=proj_f4.size()[-2:], mode='bilinear',
                                                          align_corners=True)
                psp_cat_aux1 = torch.cat([psp_cat_aux1, pooled_resized_aux1_output], dim=1)

            psp_idx = psp_idx + square

        x = self.cls(psp_cat)
        if self.zoom_factor != 1:
            x = F.interpolate(x, size=(h, w), mode='bilinear', align_corners=True)

        if self.training:
            aux = self.aux(f3_aux)
            tr_dec_aux1 = self.tr_dec_aux1(psp_cat_aux1)
            if self.zoom_factor != 1:
                aux = F.interpolate(aux, size=(h, w), mode='bilinear', align_corners=True)
                tr_dec_aux1 = F.interpolate(tr_dec_aux1, size=(h, w), mode='bilinear', align_corners=True)
            main_loss = self.criterion(x, y)
            aux_loss = self.criterion(aux, y)
            tr_aux1_loss = self.criterion(tr_dec_aux1, y)
            return x.max(1)[1], main_loss, aux_loss, 0.3 * tr_aux1_loss
        else:
            return x
