"""PPO as stage 1a's AI learns with it, from run stage1a-04 on.

Two changes to Stable-Baselines3's PPO, both from what went wrong in run stage1a-03 (see the
diary entry 2026-11-09-stage1-05-first-training):

* **Only the choices that matter count.** Each decision is a speed for each of 16 aircraft
  places, but a place may be empty or hold an aircraft whose speed is not the controller's to
  give (holding, or inside 7 NM on final), and there the choice does nothing. SpeedPolicy leaves
  those places out of the probability of an action (so practice only credits or blames choices
  that had an effect) and out of how uncertain the AI is (so the small reward for keeping its
  options open can't pull it towards random choices where they cost nothing).
* **It stays close to its head start.** AnchoredPPO adds a penalty for moving away from a frozen
  copy of the AI as it was after its head start: the divergence (KL) between the two, on the
  aircraft it controls. The penalty fades over the run, from anchor_start to anchor_end, so early
  practice can only move the AI where the evidence is strong, and later practice has more room.
"""

from __future__ import annotations

import copy

import numpy as np
import torch as th
from gymnasium import spaces
from stable_baselines3 import PPO
from stable_baselines3.common.policies import ActorCriticPolicy
from stable_baselines3.common.utils import explained_variance
from torch.distributions import kl_divergence
from torch.nn import functional as F

from approach.ai.env import FEATURES, FEATURES_1B
from approach.ai.vector import FEATURES_2

CONTROLLABLE = FEATURES.index("controllable")
CAN_TURN = FEATURES_1B.index("can_turn")
CAN_ADJUST = tuple(FEATURES_2.index(f) for f in ("can_heading", "can_level", "can_speed"))


def controlled(obs: th.Tensor, n_choices: int | None = None) -> th.Tensor:
    """1.0 for each choice that can have an effect, else 0.0: shape (batch, choices). The first choices are the
    speeds, one per aircraft place (those whose speed is the controller's to give); in stage 1b they are followed
    by a base turn per place (those the AI can turn now). In stage 2 there are three per place, in blocks: heading,
    level and speed adjustments (those the AI can make now)."""
    places = obs.shape[-2]
    if n_choices == 3 * places:
        return th.cat([(obs[..., k] > 0.5).float() for k in CAN_ADJUST], dim=-1)
    speeds = (obs[..., CONTROLLABLE] > 0.5).float()
    if n_choices is None or n_choices == speeds.shape[-1]:
        return speeds
    return th.cat([speeds, (obs[..., CAN_TURN] > 0.5).float()], dim=-1)


class SpeedPolicy(ActorCriticPolicy):
    """PPO's actor and critic, with empty places and aircraft the AI doesn't control left out."""

    def _latent(self, obs: th.Tensor) -> tuple[th.Tensor, th.Tensor]:
        features = self.extract_features(obs)
        if self.share_features_extractor:
            return self.mlp_extractor(features)
        pi_features, vf_features = features
        return self.mlp_extractor.forward_actor(pi_features), self.mlp_extractor.forward_critic(vf_features)

    @staticmethod
    def _masked(distribution, actions: th.Tensor, mask: th.Tensor) -> tuple[th.Tensor, th.Tensor]:
        per_place = th.stack([d.log_prob(a) for d, a in zip(distribution.distribution, th.unbind(actions, dim=1))], dim=1)
        entropy = th.stack([d.entropy() for d in distribution.distribution], dim=1)
        return (per_place * mask).sum(dim=1), (entropy * mask).sum(dim=1)

    def forward(self, obs: th.Tensor, deterministic: bool = False) -> tuple[th.Tensor, th.Tensor, th.Tensor]:
        latent_pi, latent_vf = self._latent(obs)
        values = self.value_net(latent_vf)
        distribution = self._get_action_dist_from_latent(latent_pi)
        actions = distribution.get_actions(deterministic=deterministic)
        log_prob, _ = self._masked(distribution, actions, controlled(obs, len(distribution.distribution)))
        return actions.reshape((-1, *self.action_space.shape)), values, log_prob

    def evaluate_actions(self, obs: th.Tensor, actions: th.Tensor):
        values, log_prob, entropy, _ = self.evaluate_with_distribution(obs, actions)
        return values, log_prob, entropy

    def evaluate_with_distribution(self, obs: th.Tensor, actions: th.Tensor):
        latent_pi, latent_vf = self._latent(obs)
        distribution = self._get_action_dist_from_latent(latent_pi)
        log_prob, entropy = self._masked(distribution, actions, controlled(obs, len(distribution.distribution)))
        return self.value_net(latent_vf), log_prob, entropy, distribution


def divergence(distribution, anchor_distribution, mask: th.Tensor) -> th.Tensor:
    """KL(AI || anchor) summed over the aircraft the AI controls: shape (batch,)."""
    per_place = th.stack([kl_divergence(p, q) for p, q in zip(distribution.distribution, anchor_distribution.distribution)],
                         dim=1)
    return (per_place * mask).sum(dim=1)


class AnchoredPPO(PPO):
    """PPO with a fading penalty for moving away from a frozen copy of the AI (its head start)."""

    def __init__(self, *args, anchor_start: float = 0.5, anchor_end: float = 0.05, **kwargs):
        self.anchor_start, self.anchor_end = anchor_start, anchor_end
        self.anchor: ActorCriticPolicy | None = None
        super().__init__(*args, **kwargs)

    def set_anchor(self, policy: ActorCriticPolicy) -> None:
        self.anchor = copy.deepcopy(policy)
        self.anchor.set_training_mode(False)
        for p in self.anchor.parameters():
            p.requires_grad_(False)

    def anchor_coef(self) -> float:
        return self.anchor_end + (self.anchor_start - self.anchor_end) * self._current_progress_remaining

    def _excluded_save_params(self) -> list[str]:
        return super()._excluded_save_params() + ["anchor"]

    def train(self) -> None:
        """Stable-Baselines3's PPO.train (version 2.x), plus the anchor penalty in the loss."""
        self.policy.set_training_mode(True)
        self._update_learning_rate(self.policy.optimizer)
        clip_range = self.clip_range(self._current_progress_remaining)
        if self.clip_range_vf is not None:
            clip_range_vf = self.clip_range_vf(self._current_progress_remaining)
        coef = self.anchor_coef() if self.anchor is not None else 0.0

        entropy_losses, pg_losses, value_losses, clip_fractions, anchor_kls = [], [], [], [], []
        continue_training = True
        for epoch in range(self.n_epochs):
            approx_kl_divs = []
            for rollout_data in self.rollout_buffer.get(self.batch_size):
                actions = rollout_data.actions
                if isinstance(self.action_space, spaces.Discrete):
                    actions = rollout_data.actions.long().flatten()
                obs = rollout_data.observations
                values, log_prob, entropy, distribution = self.policy.evaluate_with_distribution(obs, actions)
                values = values.flatten()
                advantages = rollout_data.advantages
                if self.normalize_advantage and len(advantages) > 1:
                    advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

                ratio = th.exp(log_prob - rollout_data.old_log_prob)
                policy_loss_1 = advantages * ratio
                policy_loss_2 = advantages * th.clamp(ratio, 1 - clip_range, 1 + clip_range)
                policy_loss = -th.min(policy_loss_1, policy_loss_2).mean()
                pg_losses.append(policy_loss.item())
                clip_fractions.append(th.mean((th.abs(ratio - 1) > clip_range).float()).item())

                if self.clip_range_vf is None:
                    values_pred = values
                else:
                    values_pred = rollout_data.old_values + th.clamp(values - rollout_data.old_values,
                                                                     -clip_range_vf, clip_range_vf)
                value_loss = F.mse_loss(rollout_data.returns, values_pred)
                value_losses.append(value_loss.item())

                entropy_loss = -th.mean(entropy)
                entropy_losses.append(entropy_loss.item())

                anchor_loss = th.zeros((), device=log_prob.device)
                if coef > 0:
                    with th.no_grad():
                        anchor_distribution = self.anchor.get_distribution(obs)
                    mask = controlled(obs, len(distribution.distribution))
                    anchor_loss = divergence(distribution, anchor_distribution, mask).mean()
                    anchor_kls.append(anchor_loss.item())

                loss = policy_loss + self.ent_coef * entropy_loss + self.vf_coef * value_loss + coef * anchor_loss

                with th.no_grad():
                    log_ratio = log_prob - rollout_data.old_log_prob
                    approx_kl_div = th.mean((th.exp(log_ratio) - 1) - log_ratio).cpu().numpy()
                    approx_kl_divs.append(approx_kl_div)
                if self.target_kl is not None and approx_kl_div > 1.5 * self.target_kl:
                    continue_training = False
                    break

                self.policy.optimizer.zero_grad()
                loss.backward()
                th.nn.utils.clip_grad_norm_(self.policy.parameters(), self.max_grad_norm)
                self.policy.optimizer.step()

            self._n_updates += 1
            if not continue_training:
                break

        explained_var = explained_variance(self.rollout_buffer.values.flatten(), self.rollout_buffer.returns.flatten())
        self.logger.record("train/entropy_loss", np.mean(entropy_losses))
        self.logger.record("train/policy_gradient_loss", np.mean(pg_losses))
        self.logger.record("train/value_loss", np.mean(value_losses))
        self.logger.record("train/approx_kl", np.mean(approx_kl_divs))
        self.logger.record("train/clip_fraction", np.mean(clip_fractions))
        self.logger.record("train/loss", loss.item())
        self.logger.record("train/explained_variance", explained_var)
        self.logger.record("train/anchor_coef", coef)
        if anchor_kls:
            self.logger.record("train/anchor_kl", np.mean(anchor_kls))
        self.logger.record("train/n_updates", self._n_updates, exclude="tensorboard")
        self.logger.record("train/clip_range", clip_range)
        self.last_anchor_kl = float(np.mean(anchor_kls)) if anchor_kls else 0.0
        self.last_entropy = float(-np.mean(entropy_losses)) if entropy_losses else 0.0
