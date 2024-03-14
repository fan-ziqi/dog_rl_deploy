import time

import lcm
import numpy as np
import torch

from rl_sim2real.lcm_types.pd_tau_targets_lcmt import pd_tau_targets_lcmt

lc = lcm.LCM("udpm://239.255.76.67:7667?ttl=255")


def class_to_dict(obj) -> dict:
    if not hasattr(obj, "__dict__"):
        return obj
    result = {}
    for key in dir(obj):
        if key.startswith("_") or key == "terrain":
            continue
        element = []
        val = getattr(obj, key)
        if isinstance(val, list):
            for item in val:
                element.append(class_to_dict(item))
        else:
            element = class_to_dict(val)
        result[key] = element
    return result


class LCMAgent():
    def __init__(self, cfg, se, command_profile):
        if not isinstance(cfg, dict):
            cfg = class_to_dict(cfg)
        self.cfg = cfg
        self.se = se
        self.command_profile = command_profile

        self.dt = self.cfg["control"]["decimation"] * self.cfg["sim"]["dt"]
        self.timestep = 0

        self.num_obs = self.cfg["env"]["num_observations"]
        self.num_envs = 1
        self.num_privileged_obs = self.cfg["env"]["num_privileged_obs"]
        self.num_actions = self.cfg["env"]["num_actions"]
        self.num_commands = self.cfg["commands"]["num_commands"]
        self.device = 'cuda:0'

        if "obs_scales" in self.cfg.keys():
            self.obs_scales = self.cfg["obs_scales"]
        else:
            self.obs_scales = self.cfg["normalization"]["obs_scales"]

        self.commands_scale = np.array(
            [self.obs_scales["lin_vel"], self.obs_scales["lin_vel"],
             self.obs_scales["ang_vel"], 1
             ])[:self.num_commands]


        joint_names = [
            "FL_hip_joint", "FL_thigh_joint", "FL_calf_joint",
            "FR_hip_joint", "FR_thigh_joint", "FR_calf_joint",
            "RL_hip_joint", "RL_thigh_joint", "RL_calf_joint",
            "RR_hip_joint", "RR_thigh_joint", "RR_calf_joint", ]
        self.default_dof_pos = np.array([self.cfg["init_state"]["default_joint_angles"][name] for name in joint_names])
        try:
            self.default_dof_pos_scale = np.array([self.cfg["init_state"]["default_hip_scales"], self.cfg["init_state"]["default_thigh_scales"], self.cfg["init_state"]["default_calf_scales"],
                                                   self.cfg["init_state"]["default_hip_scales"], self.cfg["init_state"]["default_thigh_scales"], self.cfg["init_state"]["default_calf_scales"],
                                                   self.cfg["init_state"]["default_hip_scales"], self.cfg["init_state"]["default_thigh_scales"], self.cfg["init_state"]["default_calf_scales"],
                                                   self.cfg["init_state"]["default_hip_scales"], self.cfg["init_state"]["default_thigh_scales"], self.cfg["init_state"]["default_calf_scales"]])
        except KeyError:
            self.default_dof_pos_scale = np.ones(12)
        self.default_dof_pos = self.default_dof_pos * self.default_dof_pos_scale

        self.p_gains = np.zeros(12)
        self.d_gains = np.zeros(12)
        for i in range(12):
            joint_name = joint_names[i]
            found = False
            for dof_name in self.cfg["control"]["stiffness"].keys():
                if dof_name in joint_name:
                    self.p_gains[i] = self.cfg["control"]["stiffness"][dof_name]
                    self.d_gains[i] = self.cfg["control"]["damping"][dof_name]
                    found = True
            if not found:
                self.p_gains[i] = 0.
                self.d_gains[i] = 0.
                if self.cfg["control"]["control_type"] in ["P", "V"]:
                    print(f"PD gain of joint {joint_name} were not defined, setting them to zero")

        print(f"p_gains: {self.p_gains}")

        self.commands = np.zeros((1, self.num_commands))
        self.actions = torch.zeros(12)
        self.last_actions = torch.zeros(12)
        self.gravity_vector = np.zeros(3)
        self.dof_pos = np.zeros(12)
        self.dof_vel = np.zeros(12)
        self.body_linear_vel = np.zeros(3)
        self.body_angular_vel = np.zeros(3)
        self.joint_pos_target = np.zeros(12)
        self.joint_vel_target = np.zeros(12)
        self.torques = np.zeros(12)
        self.contact_state = np.ones(4)

        self.joint_idxs = self.se.joint_idxs

        self.gait_indices = torch.zeros(self.num_envs, dtype=torch.float)
        self.clock_inputs = torch.zeros(self.num_envs, 4, dtype=torch.float)

        if "obs_scales" in self.cfg.keys():
            self.obs_scales = self.cfg["obs_scales"]
        else:
            self.obs_scales = self.cfg["normalization"]["obs_scales"]

        self.is_currently_probing = False

    def set_probing(self, is_currently_probing):
        self.is_currently_probing = is_currently_probing

    def get_obs(self):

        self.gravity_vector = self.se.get_gravity_vector()
        cmds, reset_timer = self.command_profile.get_command(self.timestep * self.dt, probe=self.is_currently_probing)
        self.commands[:, :] = cmds[:self.num_commands]
        if reset_timer:
            self.reset_gait_indices()
        #else:
        #    self.commands[:, 0:3] = self.command_profile.get_command(self.timestep * self.dt)[0:3]
        self.dof_pos = self.se.get_dof_pos()
        self.dof_vel = self.se.get_dof_vel()
        self.body_linear_vel = self.se.get_body_linear_vel()
        self.body_angular_vel = self.se.get_body_angular_vel()

        ob = np.concatenate((self.body_angular_vel.reshape(1, -1) * self.obs_scales["ang_vel"],
                             self.gravity_vector.reshape(1, -1),
                             self.commands[:,:3] * self.commands_scale[:3],
                             (self.dof_pos - self.default_dof_pos).reshape(1, -1) * self.obs_scales["dof_pos"],
                             self.dof_vel.reshape(1, -1) * self.obs_scales["dof_vel"],
                             torch.clip(self.actions, -self.cfg["normalization"]["clip_actions"],
                                        self.cfg["normalization"]["clip_actions"]).cpu().detach().numpy().reshape(1, -1)
                             ), axis=1)

        return torch.tensor(ob, device=self.device).float()

    def get_privileged_observations(self):
        return None

    def publish_action(self, action, hard_reset=False):

        command_for_robot = pd_tau_targets_lcmt()
        self.joint_pos_target = (action[0, :12].detach().cpu().numpy() * self.cfg["control"]["action_scale"]).flatten()
        self.joint_pos_target[[0, 3, 6, 9]] *= self.cfg["control"]["hip_scale_reduction"]
        # self.joint_pos_target[[0, 3, 6, 9]] *= -1
        self.joint_pos_target = self.joint_pos_target
        self.joint_pos_target += self.default_dof_pos
        joint_pos_target = self.joint_pos_target[self.joint_idxs]
        self.joint_vel_target = np.zeros(12)
        # print(f'cjp {self.joint_pos_target}')

        command_for_robot.q_des = joint_pos_target
        command_for_robot.qd_des = self.joint_vel_target
        command_for_robot.kp = self.p_gains
        command_for_robot.kd = self.d_gains
        command_for_robot.tau_ff = np.zeros(12)
        command_for_robot.se_contactState = np.zeros(4)
        command_for_robot.timestamp_us = int(time.time() * 10 ** 6)
        command_for_robot.id = 0

        if hard_reset:
            command_for_robot.id = -1


        self.torques = (self.joint_pos_target - self.dof_pos) * self.p_gains + (self.joint_vel_target - self.dof_vel) * self.d_gains

        lc.publish("pd_plustau_targets", command_for_robot.encode())

    def reset(self):
        self.actions = torch.zeros(12)
        self.time = time.time()
        self.timestep = 0
        return self.get_obs()

    def reset_gait_indices(self):
        self.gait_indices = torch.zeros(self.num_envs, dtype=torch.float)

    def step(self, history_obs_buf, actions, hard_reset=False):
        clip_actions = self.cfg["normalization"]["clip_actions"]
        self.last_actions = self.actions[:]
        self.actions = torch.clip(actions[0:1, :], -clip_actions, clip_actions)
        self.publish_action(self.actions, hard_reset=hard_reset)
        time.sleep(max(self.dt - (time.time() - self.time), 0))
        if self.timestep % 100 == 0: print(f'frq: {1 / (time.time() - self.time)} Hz');
        self.time = time.time()
        obs = self.get_obs()

        history_obs_buf.insert(obs)
        history_obs = history_obs_buf.get_obs_vec(np.arange(self.cfg["env"]["include_history_steps"]))

        infos = {"joint_pos": self.dof_pos[np.newaxis, :],
                 "joint_vel": self.dof_vel[np.newaxis, :],
                 "joint_pos_target": self.joint_pos_target[np.newaxis, :],
                 "joint_vel_target": self.joint_vel_target[np.newaxis, :],
                 "body_linear_vel": self.body_linear_vel[np.newaxis, :],
                 "body_angular_vel": self.body_angular_vel[np.newaxis, :],
                 "contact_state": self.contact_state[np.newaxis, :],
                 "clock_inputs": self.clock_inputs[np.newaxis, :],
                 "body_linear_vel_cmd": self.commands[:, 0:2],
                 "body_angular_vel_cmd": self.commands[:, 2:],
                 "privileged_obs": None,
                 }

        self.timestep += 1
        return history_obs, obs, None, None, infos
