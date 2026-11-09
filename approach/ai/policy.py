"""The AI's network: it looks across the whole list of aircraft, however many there are.

Each aircraft's row (approach.ai.env.FEATURES) is embedded the same way, a learned position
says where it is in the landing order, and a small transformer lets every aircraft attend to
every other one, so "a medium 4 NM behind a heavy and closing" is understood the same way
wherever it happens. Empty rows are masked out. The result goes to PPO's actor and critic
heads (Stable-Baselines3).
"""

from __future__ import annotations

import torch
from gymnasium import spaces
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
from torch import nn


class AircraftAttention(BaseFeaturesExtractor):
    def __init__(self, observation_space: spaces.Box, dim: int = 32, heads: int = 4, layers: int = 1):
        n, f = observation_space.shape
        super().__init__(observation_space, features_dim=n * dim)
        self.embed = nn.Sequential(nn.Linear(f, dim), nn.ReLU(), nn.Linear(dim, dim))
        self.position = nn.Parameter(torch.zeros(n, dim))
        layer = nn.TransformerEncoderLayer(dim, heads, dim_feedforward=2 * dim, dropout=0.0, batch_first=True)
        self.encoder = nn.TransformerEncoder(layer, layers, enable_nested_tensor=False)

    def forward(self, obs: torch.Tensor) -> torch.Tensor:
        empty = obs[..., 0] < 0.5  # the "present" feature
        empty = empty.clone()
        empty[empty.all(dim=1), 0] = False  # attention needs at least one row
        x = self.encoder(self.embed(obs) + self.position, src_key_padding_mask=empty)
        return x.masked_fill(empty.unsqueeze(-1), 0.0).flatten(1)


POLICY_KWARGS = dict(
    features_extractor_class=AircraftAttention,
    features_extractor_kwargs=dict(dim=32, heads=4, layers=1),
    net_arch=dict(pi=[128], vf=[128]),
)
