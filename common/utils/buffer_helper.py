from typing import Deque

import pyarrow as pa
import ray
import zmq

from common.utils.buffer import PrioritizedReplayBuffer


@ray.remote
class PrioritizedReplayBufferHelper(object):
    def __init__(self, buffer_cfg: dict, comm_cfg: dict):
        self.cfg = buffer_cfg

        # unpack buffer configs
        self.max_num_updates = self.cfg["max_num_updates"]
        self.priority_alpha = self.cfg["priority_alpha"]
        self.priority_beta = self.cfg["priority_beta_start"]
        self.priority_beta_end = self.cfg["priority_beta_end"]
        self.priority_beta_increment = (
            self.priority_beta_end - self.priority_beta
        ) / self.max_num_updates

        self.batch_size = self.cfg["batch_size"]

        self.buffer = PrioritizedReplayBuffer(
            self.cfg["buffer_max_size"], self.priority_alpha
        )

        # unpack communication configs
        self.repreq_port = comm_cfg["repreq_port"]
        self.pullpush_port = comm_cfg["pullpush_port"]

        # initialize zmq sockets
        print("[Buffer]: initializing sockets..")
        self.initialize_sockets()

    def initialize_sockets(self):
        # for sending batch to learner and retrieving new priorities
        context = zmq.Context()
        self.rep_socket = context.socket(zmq.REQ)
        self.rep_socket.connect(f"tcp://127.0.0.1:{self.repreq_port}")

        # for receiving replay data from workers
        context = zmq.Context()
        self.pull_socket = context.socket(zmq.PULL)
        self.pull_socket.bind(f"tcp://127.0.0.1:{self.pullpush_port}")

    def send_batch_recv_priors(self):
        # send batch and request priorities (blocking recv)
        batch = self.buffer.sample(self.batch_size, self.priority_beta)
        batch_id = pa.serialize(batch).to_buffer()
        self.rep_socket.send(batch_id)

        # receive and update priorities
        new_priors_id = self.rep_socket.recv()
        idxes, new_priorities = pa.deserialize(new_priors_id)
        self.buffer.update_priorities(idxes, new_priorities)

    def recv_data(self):
        new_replay_data_id = False
        try:
            new_replay_data_id = self.pull_socket.recv(zmq.DONTWAIT)
        except zmq.Again:
            pass

        if new_replay_data_id:
            new_replay_data = pa.deserialize(new_replay_data_id)
            for replay_data, priorities in new_replay_data:
                self.buffer.add(*replay_data)
                self.buffer.update_priorities(
                    [(self.buffer._next_idx - 1) % self.buffer._maxsize], priorities
                )

    def run(self):
        while True:
            self.recv_data()
            if len(self.buffer) > self.batch_size:
                self.send_batch_recv_priors()
            else:
                pass
