# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/5B. Multi-lang text to semantic token modeling.ipynb.

# %% auto 0
__all__ = ['load_dataset', 'rand', 'Tunables', 'T2SEmbedding', 'Encoder', 'TSARTransformer', 'make_model']

# %% ../nbs/5B. Multi-lang text to semantic token modeling.ipynb 1
import dataclasses
import random
import math
import itertools
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.profiler import record_function

from huggingface_hub import hf_hub_download
from fastcore.basics import store_attr
from fastprogress import progress_bar

from pathlib import Path

# %% ../nbs/5B. Multi-lang text to semantic token modeling.ipynb 2
from whisperspeech.modules import *
from whisperspeech import languages, inference

# %% ../nbs/5B. Multi-lang text to semantic token modeling.ipynb 6
import re

class CharTokenizer:
    """Trivial tokenizer – just use UTF-8 bytes"""
    eot = 0
    
    def encode(self, txt):
        return list(bytes(txt.strip(), 'utf-8'))

    def decode(self, tokens):
        return bytes(tokens).decode('utf-8')
    
def tokenizer(ikey, okey, length):
    """Tokenizes a transcript"""
    tok = CharTokenizer()
    def _tokenizer(samples):
        for s in samples:
            toks = torch.tensor(tok.encode(s[ikey]))
            s[okey] = F.pad(toks, (0, length - toks.shape[-1]), value=tok.eot)
            yield s
    return _tokenizer

def ar_padder(ikey, okey, length, pad_token):
    """Pads the tokens for autoregresive training"""
    import numpy as np

    def _ar_padder(samples):
        for s in samples:
            toks = s[ikey]
            if isinstance(toks, (list, np.ndarray)): toks = torch.tensor(toks)
            toks = toks.to(torch.long)
            s['in_' +okey] = F.pad(toks, (1, length - toks.shape[-1] - 1), value=pad_token)
            s['out_'+okey] = F.pad(toks, (0, length - toks.shape[-1]), value=pad_token)
            yield s
    return _ar_padder

def char_per_seconder(txt_key, stoks_key, cps_key, stoks_per_second=25):
    """Adds the characters per second metric to the input data"""
    def _char_per_seconder(samples):
        for s in samples:
            secs = s[stoks_key].shape[-1] / stoks_per_second
            s[cps_key] = len(s[txt_key]) / secs
            yield s
    return _char_per_seconder

# %% ../nbs/5B. Multi-lang text to semantic token modeling.ipynb 7
def load_dataset(
    txt_shard_spec:str,    # transcription webdataset shards
    stoks_shard_dir:str,   # stoks webdataset base dir
    samples:int,           # samples per epoch
    txt_kind:str='small.en-txt',
    vq_codes:int=4096,
    language:str='en',
    weight:float=1,
    validation:bool=False,
    exclude_files:str=None,
):
    import webdataset as wds
    from . import utils

    shards = utils.shard_glob(txt_shard_spec)
    excludes = {x for file in exclude_files.split() for x in utils.readlines(file)} if exclude_files else set()
    
    language = languages.to_id(language)
    
    def set_language(x):
        x['language'] = language
        return x
    
    same_on_all_nodes = lambda urls: urls # will only be used for validation
    ds = wds.WebDataset(shards, resampled=not validation, nodesplitter=same_on_all_nodes).compose(
        wds.decode(),
        utils.merge_in(utils.derived_dataset('eqvad-stoks', base=txt_kind, suffix='', dir=stoks_shard_dir)),
        # discard validation samples, select samples > .5s
        wds.select(lambda s: s['__key__'] not in excludes and s['stoks.npy'].shape[-1] > 12),
        tokenizer('txt', 'ttoks', length=550),
        ar_padder('stoks.npy', 'stoks', length=750, pad_token=vq_codes-1),
        ar_padder('ttoks', 'ttoks', length=550, pad_token=CharTokenizer.eot),
        char_per_seconder('txt', 'stoks.npy', 'cps', stoks_per_second=25),
        wds.map(set_language),
        wds.to_tuple('in_ttoks', 'out_ttoks', 'language', 'cps', 'in_stoks', 'out_stoks'),
        wds.shuffle(20000, initial=20000),
        wds.batched(64)
    )
    if validation:
        ds = ds.slice(samples // 64)
    ds.total_samples = samples
    ds.stoks_len = 750
    ds.stoks_codes = vq_codes
    ds.ttoks_len = 550
    ds.weight = weight

    return ds

# %% ../nbs/5B. Multi-lang text to semantic token modeling.ipynb 12
def rand(start, end):
    return random.random() * (end - start) + start

@dataclasses.dataclass
class Tunables:
    init_std :float = 1
    embeddings_std :float = .01
    embeddings_lr_scale: float = 5
    embedding_projector_lr_scale: float = 2.5
    output_mult :float = .35
    query_mult :float = 1
    encoder_depth_ratio :float = 0.25
    causal_encoder: bool = True
    eot_dropout_p :float = .5
    cps_input: bool = True
    cps_bins: int = 32
        
    lr0 :float = 1.5e-3
    clip_gradient_norm :float = .2
    weight_decay :float = 1e-1
    warmup_steps :float = 4000

    random :bool = False

    def __post_init__(self):
        # randomize the hyperparams if requested
        if self.random:
            self.init_std = 10**rand(-1,1)
            self.embeddings_std = 10**rand(-3,-.7)
            self.embeddings_lr_scale = rand(2,6)
            self.output_mult = rand(0.25,0.65)
            self.query_mult = 2**rand(-2,3)
            self.encoder_depth_ratio = 0.25
            
            self.lr0 = rand(1,5)*1e-3
            self.clip_gradient_norm = 10**rand(-3,0)
            self.warmup_steps = 100*(10**rand(1,1.85))

# %% ../nbs/5B. Multi-lang text to semantic token modeling.ipynb 13
class T2SEmbedding(nn.Module):
    def __init__(self, length=1500, codes=1024, width=384, pos_embs=None, stoks_width=384):
        super().__init__()
        self.embedding = FlexEmbeddings(codes, width, special_codes=1, frozen_width=stoks_width)
        if pos_embs is None: pos_embs = sinusoids(length, width)
        self.register_buffer("positional_embedding", pos_embs)
    
    def forward(self, Stoks, xenc, cps=None, offset=0):
        Sembs = self.embedding(Stoks)
        xin = (Sembs + self.positional_embedding[offset : offset + Sembs.shape[1]]).to(xenc.dtype)
        if cps is not None: xin = xin + cps
        return xin, offset

# %% ../nbs/5B. Multi-lang text to semantic token modeling.ipynb 14
class Encoder(nn.Module):
    def __init__(self, depth=6, width=384, n_head=6, length=1500, codes=1024, emb_width=384, ffn_mult=4, pos_embs=None, tunables=Tunables()):
        super().__init__()
        self.emb_width = emb_width
        self.tunables = tunables
        
        self.embedding = FlexEmbeddings(codes, width, frozen_width=emb_width)

        if pos_embs is None: pos_embs = sinusoids(length, width)
        self.register_buffer("positional_embedding", pos_embs)

        self.layers = nn.ModuleList([
            ResidualAttentionBlock(width, n_head,
                                   qk_scale=tunables.query_mult*8/math.sqrt(width/n_head), ffn_mult=ffn_mult) for _ in range(depth)
        ])

        self.ln_post = LayerNorm(width)
        
        mask = torch.empty(length, length).fill_(-torch.inf).triu_(1)
        self.register_buffer("mask", mask, persistent=False)
        
    def forward(self, Stoks, positions, lang_emb=None):
        xin = self.embedding(Stoks)

        if lang_emb is not None: xin += lang_emb
        
        x = (xin +
             self.positional_embedding[positions]).to(xin.dtype)

        for l in self.layers: x = l(x, positions,
                                    causal=self.tunables.causal_encoder and self.training,
                                    mask=self.mask if self.tunables.causal_encoder and not self.training else None)
        
        return self.ln_post(x)

# %% ../nbs/5B. Multi-lang text to semantic token modeling.ipynb 15
class TSARTransformer(nn.Module):
    def __init__(self, depth=6, n_head=6, head_width=64, ffn_mult=4,
                 ttoks_len=200, ttoks_codes=256, ttoks_width=None,
                 stoks_len=1500, stoks_codes=1024, stoks_width=None,
                 tunables=Tunables()):
        super().__init__()
        store_attr("depth,n_head,head_width,ffn_mult,stoks_width,ttoks_width,ttoks_len,stoks_len,ttoks_codes,stoks_codes")

        width = n_head * head_width
        self.width = width
        self.base_width = 3 * head_width
        self.tunables = tunables
        if self.stoks_width is None: self.stoks_width = self.width
        if self.ttoks_width is None: self.ttoks_width = self.width
        
        self.lang_embeddings = nn.Embedding(len(languages.languages), width)
        if tunables.cps_input:
            self.cps_embeddings = nn.Embedding(tunables.cps_bins, self.width)
        else:
            self.cps_embeddings = None        
        
        encoder_depth = int(depth * 2 * tunables.encoder_depth_ratio)
        decoder_depth = depth * 2 - encoder_depth
        tformer_args = dict(width=width, n_head=n_head, ffn_mult=ffn_mult, tunables=tunables)
        self.encoder = Encoder(length=ttoks_len, codes=ttoks_codes, emb_width=self.ttoks_width, depth=encoder_depth, **tformer_args)
        self.embeddings = T2SEmbedding(length=stoks_len, codes=stoks_codes, width=width, stoks_width=self.stoks_width)

        self.decoder = BaseDecoder(
            length=stoks_len, 
            depth=decoder_depth,
            qk_scale=tunables.query_mult*8/math.sqrt(width/n_head),
            width=width, n_head=n_head, ffn_mult=ffn_mult,
        )
        self.tokenizer = None
        
        self.apply(self.init_transformer)

    def load_frozen_semantic_embeddings(self, vqmodel):
        self.embeddings.embedding.set_frozen_embeddings(vqmodel.rq.layers[0]._codebook.embed[0])

    def setup(self, device):
        pass

    def init_transformer(self, m):
        if isinstance(m, LinearHead):
            m.no_weight_decay = True
            torch.nn.init.constant_(m.weight, 0)
        elif isinstance(m, QueryHead):
            m.lr_scale = 1/(m.weight.shape[1] / self.base_width)
            torch.nn.init.constant_(m.weight, 0)
        elif isinstance(m, nn.Embedding):
            m.no_weight_decay = True
            m.lr_scale = self.tunables.embeddings_lr_scale
            std = self.tunables.embeddings_std
            torch.nn.init.trunc_normal_(m.weight, std=std, a=-3*std, b=3*std)
        elif isinstance(m, EmbeddingProjector):
            m.lr_scale = self.tunables.embedding_projector_lr_scale
            std = self.tunables.init_std
            torch.nn.init.trunc_normal_(m.weight, std=std, a=-3*std, b=3*std)
        elif isinstance(m, nn.Linear):
            m.lr_scale = 1/(m.weight.shape[1] / self.base_width)
            std = self.tunables.init_std / m.weight.shape[1]
            torch.nn.init.trunc_normal_(m.weight, std=std, a=-3*std, b=3*std)
            if m.bias is not None:
                torch.nn.init.trunc_normal_(m.bias, std=std, a=-3*std, b=3*std)
        elif isinstance(m, nn.LayerNorm):
            m.no_weight_decay = True
            torch.nn.init.constant_(m.bias, 0)
            torch.nn.init.constant_(m.weight, 1)
    
    def _embed_cps(self, cpss):
        if self.cps_embeddings is None: return None

        cps_bin = (cpss / 20 * self.tunables.cps_bins).to(torch.long)
        cps_bin[cps_bin >= self.tunables.cps_bins] = self.tunables.cps_bins-1
        return self.cps_embeddings(cps_bin).unsqueeze(1)

    def run_encoder(self, in_ttoks, languages, cpss):
        if len(languages.shape) != 3: lang_embs = self.lang_embeddings(languages)
        else: lang_embs = languages
        if len(lang_embs.shape) == 2: lang_embs = lang_embs.unsqueeze(1)
        
        cps_emb = self._embed_cps(cpss)

        with record_function("encoder"):
            positions = torch.arange(0, in_ttoks.shape[1], device=in_ttoks.device)
            xenc = self.encoder(in_ttoks.to(torch.long), positions, lang_emb=lang_embs)

        return xenc, positions, cps_emb
    
    def forward(self, in_ttoks, out_ttoks, languages, cpss, in_stoks, out_stoks=None, in_stoks_positions=None, loss=True, offset=None, xenc=None, xenc_positions=None, cps_emb=None):
        if xenc is None:
            xenc, xenc_positions, cps_emb = self.run_encoder(in_ttoks, languages, cpss)

        with record_function("decoder"):
            x = (self.embeddings.embedding(in_stoks) + 
                 self.embeddings.positional_embedding[in_stoks_positions] +
                 cps_emb).to(xenc[0].dtype)
            x = self.decoder(x, in_stoks_positions, xenc, xenc_positions)
            logits = self.embeddings.embedding.unembed(x)
            logits = logits * self.tunables.output_mult / (self.width / self.base_width)

        if loss is not None:
            with record_function("loss"):
                loss = F.cross_entropy(logits.transpose(-1,-2), out_stoks)
                if self.training and self.tunables.causal_encoder:
                    enc_logits = self.encoder.embedding.unembed(xenc)
                    enc_logits = enc_logits * self.tunables.output_mult / (self.width / self.base_width)
                    loss += 0.1 * F.cross_entropy(enc_logits.transpose(-1,-2), out_ttoks)

        return logits, loss

    #
    # inference
    #
    @classmethod
    def load_model(cls, ref="collabora/whisperspeech:t2s-small-en+pl.model",
                   repo_id=None, filename=None, local_filename=None, device=None):
        if repo_id is None and filename is None and local_filename is None:
            if ":" in ref:
                repo_id, filename = ref.split(":", 1)
            else:
                local_filename = ref
        if not local_filename:
            local_filename = hf_hub_download(repo_id=repo_id, filename=filename)
        spec = torch.load(local_filename, map_location=device)
        model = cls(**spec['config'], tunables=Tunables(**spec['tunables']))
        model.load_state_dict(spec['state_dict'])
        model.eval().to(device)
        return model

    def load_checkpoint(self, local_filename_or_obj):
        if isinstance(local_filename_or_obj, (str, Path)):
            spec = torch.load(local_filename, map_location='cpu')
        else:
            spec = local_filename_or_obj
        assert 'pytorch-lightning_version' in spec, 'not a valid PyTorch Lightning checkpoint'
        state_dict = {k.replace('model.', ''):v
                      for k,v in spec['state_dict'].items()}
        self.load_state_dict(state_dict)
        return self

    def save_model(self, fname):
        torch.save(dict(config = self.__stored_args__,
                        tunables = dataclasses.asdict(self.tunables),
                        state_dict = self.state_dict()), fname)

    def ensure_tokenizer(self):
        assert not self.training
        if self.tokenizer is None: self.tokenizer = CharTokenizer()

    def switch_dtypes(self, dtype=torch.float16):
        self.dtype = dtype
        for n,m in self.named_modules():
            # convert every leaf layer apart from the LayerNorms
            if isinstance(m, (nn.Linear, nn.Embedding)):
                m.to(dtype)
            # take care of buffers ([kv]_cache, masks) that are not in the leaf layers
            for bn,b in m.named_buffers(recurse=False):
                setattr(m,bn,b.to(dtype))

    def optimize(self, max_batch_size=1, dtype=torch.float16, torch_compile=True):
        for emb in [self.embeddings.embedding, self.embeddings.embedding]:
            emb.convert_for_eval()
        for l in self.encoder.layers:
            l.attn.convert_for_eval()
        for l in self.decoder.layers:
            l.attn.convert_for_eval()
            l.cross_attn.convert_for_eval()
            l.setup_kv_cache(max_batch_size, self.stoks_len, self.ttoks_len)
        self.switch_dtypes(dtype)
        if torch_compile:
            self.generate_next = torch.compile(self.generate_next, mode="reduce-overhead", fullgraph=True)

    @property
    def device(self):
        return next(self.parameters()).device

    def generate_one(self, toks, toks_positions, cps_emb, xenc, xenc_positions, T, top_k):
        probs, _ = self(None, None, None, None, toks, in_stoks_positions=toks_positions, loss=None, xenc=xenc, xenc_positions=xenc_positions, cps_emb=cps_emb)
        probs = probs[:,-1]
        probs[self.embeddings.embedding.codes:] = -torch.inf
        return inference.sample(probs, T, top_k)

    def generate_next(self, *args, **kwargs):
        return self.generate_one(*args, **kwargs)

    @torch.no_grad()
    def prep(self, txt, cps=15, lang="en"):
        dev = self.device
        ttoks = torch.tensor(self.tokenizer.encode(txt), device=dev)
        ttoks = F.pad(ttoks, (0, self.ttoks_len - len(ttoks)), value=self.tokenizer.eot).unsqueeze(0)
        cpss = torch.tensor([cps], device=dev)
        langs = torch.tensor([languages.to_id(lang)], device=dev)
        return ttoks, cpss, langs
    
    @torch.no_grad()
    def generate(self, txt, cps=15, lang="en", N=None, bs=1, T=0.7, top_k=None, step=None, show_progress_bar=True):
        self.ensure_tokenizer()
        N = N or self.stoks_len
        dev = self.device
        ttoks = []
        langs = []
        if isinstance(lang, list):
            lang0 = lang[0]
            assert isinstance(txt, list), "lang and txt have to be both lists or strings"
            for txt, lang in zip(txt, lang):
                tt = self.tokenizer.encode(txt)
                ttoks += tt
                langs += [languages.to_id(lang)] * len(tt)
        elif isinstance(lang, torch.Tensor):
            langs = lang
            ttoks = self.tokenizer.encode(txt)
        else:
            lang0 = lang
            ttoks = self.tokenizer.encode(txt)
            langs = torch.tensor([languages.to_id(lang)], device=dev).unsqueeze(0)
        ttoks = torch.tensor(ttoks, device=dev)
        ttoks = F.pad(ttoks, (1, self.ttoks_len - len(ttoks) - 1), value=self.tokenizer.eot).unsqueeze(0)
        cpss = torch.tensor([cps], device=dev)
        if not isinstance(langs, torch.Tensor):
            langs = torch.tensor(langs, device=dev)
            langs = F.pad(langs, (1, self.ttoks_len - len(langs) - 1), value=languages.to_id(lang0)).unsqueeze(0)
        it = range(0,N-1)
        if show_progress_bar: it = progress_bar(it)

        toks = torch.zeros((bs,N), dtype=torch.long, device=dev)
        toks[:,0] = self.stoks_codes-1
        toks_positions = torch.arange(N, device=dev)
        with record_function("encode"):
            ttoks, langs, cpss = [x.repeat(bs, 1) for x in (ttoks, langs, cpss)]
            xenc, xenc_positions, cps_emb = self.run_encoder(ttoks, langs, cpss)
            toks_positions = torch.arange(N+1, device=dev)
        # contrary to S2A this model works without prefill and is actually a tiny bit faster
        # with record_function("prefill"):
        #     toks[0,1] = self.generate_one(toks[:,:1], toks_positions[:1], cps_emb, xenc, xenc_positions, T, top_k)

        with inference.inference_context():
            for i in it:
                toks[:,i+1] = self.generate_next(toks[:,i:i+1], toks_positions[i:i+1], cps_emb, xenc, xenc_positions, T, top_k)[:,0]
                if i % 25 == 0 and (toks[:,i+1] == self.stoks_codes-1).all(): return toks[:,:i+1]

                # for profiling, debugging or early exit
                if step is not None: step()
        return toks[:,:]
    
    @torch.no_grad()
    def generate_batch(self, txts, N=None, T=1.1, top_k=7, show_progress_bar=True):
        self.ensure_tokenizer()
        N = self.stoks_len
        dev = self.device
        ttoks = []
        for txt in txts:
            ttoks_ = torch.tensor(self.tokenizer.encode(txt), device=dev)
            ttoks_ = F.pad(ttoks_, (0, self.ttoks_len - len(ttoks_)), value=self.tokenizer.eot).unsqueeze(0)
            ttoks.append(ttoks_)
        ttoks = torch.cat(ttoks, dim=0)
        toks = torch.zeros((len(ttoks),N), dtype=torch.long, device=dev)
        it = range(N)
        if show_progress_bar: it = progress_bar(it)
        for i in it:
            p, _ = self(ttoks, toks[:,:i], loss=None)
            last_p = p[:,-1]
            if top_k:
                last_p[last_p < torch.topk(last_p, top_k).values[:,-1,None]] = -torch.inf
            tok = torch.multinomial((last_p / float(T)).softmax(-1), 1)
            toks[:,i] = tok[:,0]
            if (toks[:,i] == self.stoks_codes-1).all(): return toks[:,:i]
        return toks

# %% ../nbs/5B. Multi-lang text to semantic token modeling.ipynb 16
def _make_model(size:str, tunables:Tunables=Tunables(), dataset=None, **kwargs):
    kwargs = dict(stoks_len = dataset.stoks_len, ttoks_len = dataset.ttoks_len, tunables=tunables, **kwargs)
    if 'stoks_codes' not in kwargs: kwargs['stoks_codes'] = dataset.stoks_codes
    if size == 'micro':
        return TSARTransformer(depth=2, n_head=3, ffn_mult=1, **kwargs)
    if size == 'tiny':
        return TSARTransformer(depth=4, n_head=6, **kwargs)
    if size == 'base':
        return TSARTransformer(depth=6, n_head=8, **kwargs)
    if size == 'small':
        return TSARTransformer(depth=12, n_head=12, **kwargs)
    if size == 'small+':
        return TSARTransformer(depth=12, n_head=16, **kwargs)
    if size == 'medium':
        return TSARTransformer(depth=24, n_head=16, **kwargs)

def make_model(size:str, frozen_embeddings_model:str=None, tunables:Tunables=Tunables(), dataset:torch.utils.data.Dataset=None):
    from . import vq_stoks

    if frozen_embeddings_model:
        vqmodel = vq_stoks.RQBottleneckTransformer.load_model(frozen_embeddings_model)
        model = _make_model(size, tunables, dataset, stoks_codes=vqmodel.vq_codes+1, stoks_width=vqmodel.rq.layers[0]._codebook.embed[0].shape[-1])
        model.load_frozen_semantic_embeddings(vqmodel)
    else:
        model = _make_model(size, tunables, dataset, mode=mode)
    return model
