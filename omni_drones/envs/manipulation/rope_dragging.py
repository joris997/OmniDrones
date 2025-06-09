# MIT License
#
# Copyright (c) 2025 Joris and Alejandro, KTH 
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


import torch
import torch.distributions as D
from torch.func import vmap
from omni.isaac.core.objects import DynamicCuboid, DynamicCylinder
from tensordict.tensordict import TensorDict, TensorDictBase
from torchrl.data import CompositeSpec, UnboundedContinuousTensorSpec, DiscreteTensorSpec

import omni_drones.utils.kit as kit_utils
import omni_drones.utils.scene as scene_utils

from omni_drones.envs.isaac_env import AgentSpec, IsaacEnv
from omni_drones.views import RigidPrimView
from omni_drones.utils.torch import cpos, off_diag, others, quat_axis, euler_to_quaternion
from omni_drones.robots.drone import MultirotorBase

from .utils import ManipulationGroup, ManipulationCfg


class RopeDragging(IsaacEnv):
    r"""
    A cooperative control task where two quadcopters are connected by a rope and,
    via interaction between the rope and an object, move the object to the goal location

    ## Observation

    The observation space is specified a py:class:`CompositeSpec` containing the following items:

    - `obs_self` (1, \*): The state of each UAV observed by itself, containing its kinematic
      information with the position being relative to the payload. It also includes a one-hot
      vector indicating each drone's identity.
    - `obs_others` (k-1, \*): The observed states of other agents.
    - `obs_payload` (1, \*): The state of the frame, containing its position (relative to the
      reference), rotation (in quaternions and direction vectors), and velocities

    ## Reward

    - `separation`: A factor that penalizes all agents when the minimum separation is too small.
    - `pos`: Reward for hovering at the reference position, computed as :math:`\exp(-a * \text{pos_error})`.
    - `up`: Reward for keeping the payload upright.
    - `swing`: Reward for avoid large swinging of the payload.
    - `joint_limit`: Reward for keeping the joint states in a reasonable range to avoid glitchy behaviors.
    - `effort`: Reward computed from the effort of the drone to optimize the
      energy consumption.

    ```{math}
        r = r_\text{separation} * (r_\text{pos} + r_\text{pos} * (r_\text{up} + r_\text{swing}) + r_\text{joint\_limit})
    ```

    ## Config

    | Parameter       | Type               | Default       | Description                                                                                                                       |
    | --------------- | ------------------ | ------------- | --------------------------------------------------------------------------------------------------------------------------------- |
    | `drone_model`   | str                | "crazyflie"   |                                                                                                                                   |
    | `num_drones`    | int                | 2             |                                                                                                                                   |
    | `safe_distance` | float              | 0.5           | A threshold value that gives penalty when the minimum separation between the UAVs is too small.                                   |
    | `mass_scale`    | List[float, float] | [0.5, 0.8]    | A tuple of two values that specifies the range of the payload mass to sample from in each episode (as ratio to the drone's mass). |
    """
    def __init__(self, cfg, headless):
        self.reward_effort_weight = cfg.task.reward_effort_weight
        self.reward_spin_weight = cfg.task.reward_spin_weight
        self.reward_swing_weight = cfg.task.reward_swing_weight
        self.reward_action_smoothness_weight = cfg.task.reward_action_smoothness_weight
        self.reward_distance_scale = cfg.task.reward_distance_scale
        self.time_encoding = cfg.task.time_encoding
        self.safe_distance = cfg.task.safe_distance

        super().__init__(cfg, headless)

        self.group.initialize()
        self.payload = self.group.payload_view

        self.payload_target_visual = RigidPrimView(
            "/World/envs/.*/payloadTargetVis",
            reset_xform_properties=False
        )
        self.payload_target_visual.initialize()

        self.init_poses = self.group.get_world_poses(clone=True)
        self.init_velocities = torch.zeros_like(self.group.get_velocities())
        self.init_joint_pos = self.group.get_joint_positions(clone=True)
        self.init_joint_vel = torch.zeros_like(self.group.get_joint_velocities())

        self.init_drone_poses = self.drone.get_world_poses(clone=True)
        self.init_drone_vels = torch.zeros_like(self.drone.get_velocities())

        # Bounds for the payload rotation
        self.payload_target_rpy_dist = D.Uniform(
            torch.tensor([0., 0., 0.], device=self.device) * torch.pi,
            torch.tensor([0., 0., 0.], device=self.device) * torch.pi
        )
        # Bounds for the payload mass
        payload_mass_scale = self.cfg.task.payload_mass_scale
        self.payload_mass_dist = D.Uniform(
            torch.as_tensor(payload_mass_scale[0] * self.drone.MASS_0.sum(), device=self.device),
            torch.as_tensor(payload_mass_scale[1] * self.drone.MASS_0.sum(), device=self.device)
        )
        # Bounds for the initial position of the drones
        self.init_pos_dist = D.Uniform(
            torch.tensor([-3, -3, 1.], device=self.device),
            torch.tensor([3., 3., 2.5], device=self.device)
        )
        # Bounds for the initial rotation of the drones
        self.init_rpy_dist = D.Uniform(
            torch.tensor([0., 0., 0.], device=self.device) * torch.pi,
            torch.tensor([0., 0., 2.], device=self.device) * torch.pi
        )
        self.payload_target_pos = torch.tensor([0., 0., 2.], device=self.device)
        self.payload_target_heading = torch.zeros(self.num_envs, 3, device=self.device)
        self.last_distance = torch.zeros(self.num_envs, 1, device=self.device)

        self.alpha = 0.8

    def _design_scene(self):
        drone_model_cfg = self.cfg.task.drone_model
        self.drone, self.controller = MultirotorBase.make(
            drone_model_cfg.name, drone_model_cfg.controller
        )

        group_cfg = ManipulationCfg(num_drones=self.cfg.task.num_drones)
        self.group = ManipulationGroup(drone=self.drone, cfg=group_cfg)

        scene_utils.design_scene()

        DynamicCylinder(
            "/World/envs/env_0/payloadTargetVis",
            translation=torch.tensor([0., 0., 0.]),
            scale=torch.tensor([0.1, 0.1, 0.02]),
            color=torch.tensor([0.1, 0.8, 0.1])
        )
        # DynamicCuboid(
        #     "/World/envs/env_0/payloadTargetVis",
        #     translation=torch.tensor([0., 0., 2.]),
        #     scale=torch.tensor([0.75, 0.5, 0.2]),
        #     color=torch.tensor([0.8, 0.1, 0.1]),
        #     size=2.01,
        # )
        kit_utils.set_collision_properties(
            "/World/envs/env_0/payloadTargetVis",
            collision_enabled=False
        )
        kit_utils.set_rigid_body_properties(
            "/World/envs/env_0/payloadTargetVis",
            disable_gravity=True
        )

        self.group.spawn(translations=[(0, 0, 0.5)], enable_collision=False)
        return ["/World/defaultGroundPlane"]

    def _set_specs(self):
        drone_state_dim = self.drone.state_spec.shape[-1] + self.drone.n
        payload_state_dim = 22
        if self.time_encoding:
            self.time_encoding_dim = 4
            payload_state_dim += self.time_encoding_dim

        # Define the observation space
        observation_spec = CompositeSpec({
            "obs_self": UnboundedContinuousTensorSpec((1, drone_state_dim)),
            "obs_others": UnboundedContinuousTensorSpec((self.drone.n-1, 13+1)),
            "obs_payload": UnboundedContinuousTensorSpec((1, payload_state_dim)),
        }).to(self.device)
        observation_central_spec = CompositeSpec({
            "state_drones": UnboundedContinuousTensorSpec((self.drone.n, drone_state_dim)),
            "state_payload": UnboundedContinuousTensorSpec((1, payload_state_dim)),
        }).to(self.device)
        self.observation_spec = CompositeSpec({
            "agents": {
                "observation": observation_spec.expand(self.drone.n),
                "observation_central": observation_central_spec,
            }
        }).expand(self.num_envs).to(self.device)

        # Define the action space
        self.action_spec = CompositeSpec({
            "agents": {
                "action": torch.stack([self.drone.action_spec] * self.drone.n, dim=0),
            }
        }).expand(self.num_envs).to(self.device)

        # Define the reward spec
        self.reward_spec = CompositeSpec({
            "agents": {
                "reward": UnboundedContinuousTensorSpec((self.drone.n, 1))
            }
        }).expand(self.num_envs).to(self.device)
        self.agent_spec["drone"] = AgentSpec(
            "drone", self.drone.n,
            observation_key=("agents", "observation"),
            action_key=("agents", "action"),
            reward_key=("agents", "reward"),
            state_key=("agents", "state")
        )

        stats_spec = CompositeSpec({
            "return": UnboundedContinuousTensorSpec(self.drone.n),
            "episode_len": UnboundedContinuousTensorSpec(1),
            "pos_error": UnboundedContinuousTensorSpec(1),
            "heading_alignment": UnboundedContinuousTensorSpec(1),
            "uprightness": UnboundedContinuousTensorSpec(1),
            "action_smoothness": UnboundedContinuousTensorSpec(self.drone.n),
        }).expand(self.num_envs).to(self.device)
        self.observation_spec["stats"] = stats_spec
        self.stats = stats_spec.zero()

    def _reset_idx(self, env_ids: torch.Tensor):
        pos = self.init_pos_dist.sample(env_ids.shape)
        rpy = self.init_rpy_dist.sample(env_ids.shape)
        rot = euler_to_quaternion(rpy)
        heading = quat_axis(rot, 0)
        # pos = torch.tensor([[0.0, 0.0, 2.0]], device='cuda')

        #? Drone stuff?
        self.group._reset_idx(env_ids)
        self.group.set_world_poses(pos + self.envs_positions[env_ids], rot, env_ids)
        self.group.set_velocities(self.init_velocities[env_ids], env_ids)

        self.group.set_joint_positions(self.init_joint_pos[env_ids], env_ids)
        self.group.set_joint_velocities(self.init_joint_vel[env_ids], env_ids)

        # Payload stuff
        payload_target_rpy = self.payload_target_rpy_dist.sample(env_ids.shape)
        payload_target_rot = euler_to_quaternion(payload_target_rpy)
        payload_target_heading = quat_axis(payload_target_rot, 0)
        payload_masses = self.payload_mass_dist.sample(env_ids.shape)

        self.payload_target_heading[env_ids] = payload_target_heading

        self.payload.set_masses(payload_masses, env_ids)
        self.payload_target_visual.set_world_poses(
            # We can change the target position here
            positions=torch.tensor([[0., 0., 0.]],device='cuda').reshape(-1,3),
            orientations=payload_target_rot,
            env_indices=env_ids
        )

        self.stats[env_ids] = 0.
        distance = torch.cat([
            self.payload_target_pos-pos,
            payload_target_heading-heading,
        ], dim=-1).norm(dim=-1, keepdim=True)
        self.last_distance[env_ids] = distance

    def _pre_sim_step(self, tensordict: TensorDictBase):
        actions = tensordict[("agents", "action")]
        self.effort = self.drone.apply_action(actions)

    def _compute_state_and_obs(self):
        self.drone_states = self.drone.get_state()
        self.group.get_state()
        payload_vels = self.payload.get_velocities()
        drone_pos = self.drone_states[..., :3]

        self.payload_pos, self.payload_rot = self.get_env_poses(self.payload.get_world_poses())
        self.payload_heading: torch.Tensor = quat_axis(self.payload_rot, axis=0)
        self.payload_up: torch.Tensor = quat_axis(self.payload_rot, axis=2)

        self.drone_rpos = vmap(cpos)(drone_pos, drone_pos)
        self.drone_rpos = vmap(off_diag)(self.drone_rpos)
        self.drone_pdist = torch.norm(self.drone_rpos, dim=-1, keepdim=True)
        payload_drone_rpos = self.payload_pos.unsqueeze(1) - drone_pos

        self.target_payload_rpose = torch.cat([
            self.payload_target_pos - self.payload_pos,
            self.payload_target_heading - self.payload_heading
        ], dim=-1)

        payload_state = [
            self.target_payload_rpose,
            self.payload_rot,  # 4
            payload_vels,  # 6
            self.payload_heading,  # 3
            self.payload_up, # 3
        ]
        if self.time_encoding:
            t = (self.progress_buf / self.max_episode_length).unsqueeze(-1)
            payload_state.append(t.expand(-1, self.time_encoding_dim))
        payload_state = torch.cat(payload_state, dim=-1).unsqueeze(1)

        obs = TensorDict({}, [self.num_envs, self.drone.n])
        identity = torch.eye(self.drone.n, device=self.device).expand(self.num_envs, -1, -1)
        obs["obs_self"] = torch.cat(
            [-payload_drone_rpos, self.drone_states[..., 3:], identity], dim=-1
        ).unsqueeze(2) # [..., 1, state_dim]
        obs["obs_others"] = torch.cat(
            [self.drone_rpos, self.drone_pdist, vmap(others)(self.drone_states[..., 3:13])], dim=-1
        ) # [..., n-1, state_dim + 1]
        obs["obs_payload"] = payload_state.expand(-1, self.drone.n, -1).unsqueeze(2) # [..., 1, 22]

        state = TensorDict({}, self.num_envs)
        state["state_payload"] = payload_state # [..., 1, 22]
        state["state_drones"] = obs["obs_self"].squeeze(2) # [..., n, state_dim]

        self.pos_error = self.target_payload_rpose[..., :3].norm(dim=-1, keepdim=True)
        self.heading_alignment = torch.sum(
            self.payload_heading * self.payload_target_heading, dim=-1, keepdim=True
        )

        return TensorDict(
            {
                "agents": {
                    "observation": obs,
                    "observation_central": state,
                },
                "stats": self.stats.clone(),
            },
            self.num_envs,
        )

    def _compute_reward_and_done(self):
        vels = self.payload.get_velocities()
        joint_positions = (
            self.group.get_joint_positions()[..., :16]
            / self.group.joint_limits[..., :16, 0].abs()
        )

        distance = torch.norm(self.target_payload_rpose, dim=-1, keepdim=True)
        separation = self.drone_pdist.min(dim=-2).values.min(dim=-2).values

        reward = torch.zeros(self.num_envs, self.drone.n, 1, device=self.device)
        reward_pose = torch.exp(-distance * self.reward_distance_scale)

        up = self.payload_up[:, 2]
        reward_up = torch.square((up + 1) / 2).unsqueeze(-1)

        spinnage = vels[:, -3:].abs().sum(-1, keepdim=True)
        reward_spin = self.reward_spin_weight * torch.exp(-torch.square(spinnage))

        swing = vels[:, :3].abs().sum(-1, keepdim=True)
        reward_swing = self.reward_swing_weight * torch.exp(-torch.square(swing))

        reward_effort = self.reward_effort_weight * torch.exp(-self.effort).mean(-1, keepdim=True)
        reward_separation = torch.square(separation / self.safe_distance).clamp(0, 1)
        reward_joint_limit = 0.5 * torch.mean(1 - torch.square(joint_positions), dim=-1)

        reward_action_smoothness = self.reward_action_smoothness_weight * -self.drone.throttle_difference

        reward[:] = (
            reward_separation * (
                reward_pose
                + reward_pose * (reward_up + reward_spin + reward_swing)
                + reward_joint_limit
                + reward_action_smoothness.mean(1, True)
                + reward_effort
            )
        ).unsqueeze(-1)

        misbehave = (self.drone_states[..., 2] < 0.2).any(-1, keepdim=True)
        hasnan = torch.isnan(self.drone_states).any(-1)

        terminated = misbehave | hasnan.any(-1, keepdim=True)
        truncated = (self.progress_buf >= self.max_episode_length).unsqueeze(-1)

        self.stats["return"].add_(reward.mean(1))
        self.stats["episode_len"][:] = self.progress_buf.unsqueeze(-1)
        self.stats["pos_error"].lerp_(self.pos_error, (1-self.alpha))
        self.stats["heading_alignment"].lerp_(self.heading_alignment, (1-self.alpha))
        self.stats["uprightness"].lerp_(self.payload_up[:, 2].unsqueeze(-1), (1-self.alpha))
        self.stats["action_smoothness"].lerp_(-self.drone.throttle_difference, (1-self.alpha))

        return TensorDict(
            {
                "agents": {
                    "reward": reward,
                },
                "done": terminated | truncated,
                "terminated": terminated,
                "truncated": truncated,
            },
            self.batch_size,
        )
