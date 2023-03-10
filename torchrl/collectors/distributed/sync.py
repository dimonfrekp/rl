# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

r"""Generic distributed data-collector using torch.distributed backend."""

import os
import socket
from copy import deepcopy
from datetime import timedelta
from typing import OrderedDict

import torch.cuda
from tensordict import TensorDict
from torch import multiprocessing as mp, nn

from torchrl.collectors import MultiaSyncDataCollector
from torchrl.collectors.collectors import (
    _DataCollector,
    MultiSyncDataCollector,
    SyncDataCollector,
)
from torchrl.data.utils import CloudpickleWrapper
from torchrl.envs import EnvBase, EnvCreator

SUBMITIT_ERR = None
try:
    import submitit

    _has_submitit = True
except ModuleNotFoundError as err:
    _has_submitit = False
    SUBMITIT_ERR = err

MAX_TIME_TO_CONNECT = 1000
DEFAULT_SLURM_CONF = {
    "timeout_min": 10,
    "slurm_partition": "train",
    "slurm_cpus_per_task": 32,
    "slurm_gpus_per_node": 0,
}


def _distributed_init_collection_node(
    rank,
    rank0_ip,
    tcpport,
    world_size,
    backend,
    collector_class,
    num_workers,
    env_make,
    policy,
    frames_per_batch,
    collector_kwargs,
    update_interval,
    total_frames,
    verbose=False,
):
    os.environ["MASTER_ADDR"] = str(rank0_ip)
    os.environ["MASTER_PORT"] = str(tcpport)

    if verbose:
        print(f"node with rank {rank} -- creating collector of type {collector_class}")
    if not issubclass(collector_class, SyncDataCollector):
        env_make = [env_make] * num_workers
    else:
        collector_kwargs["return_same_td"] = True
        if num_workers != 1:
            raise RuntimeError(
                "SyncDataCollector and subclasses can only support a single environment."
            )

    if isinstance(policy, nn.Module):
        policy_weights = TensorDict(dict(policy.named_parameters()), [])
        # TODO: Do we want this?
        # updates the policy weights to avoid them to be shared
        if all(
            param.device == torch.device("cpu") for param in policy_weights.values()
        ):
            policy = deepcopy(policy)
            policy_weights = TensorDict(dict(policy.named_parameters()), [])

        policy_weights = policy_weights.apply(lambda x: x.data)
    else:
        policy_weights = TensorDict({}, [])

    collector = collector_class(
        env_make,
        policy,
        frames_per_batch=frames_per_batch,
        split_trajs=False,
        total_frames=total_frames,
        **collector_kwargs,
    )

    print("IP address:", rank0_ip, "\ttcp port:", tcpport)
    if verbose:
        print(f"node with rank {rank} -- launching distributed")
    torch.distributed.init_process_group(
        backend,
        rank=rank,
        world_size=world_size,
        timeout=timedelta(MAX_TIME_TO_CONNECT),
        # init_method=f"tcp://{rank0_ip}:{tcpport}",
    )
    if verbose:
        print(f"node with rank {rank} -- creating store")
    if verbose:
        print(f"node with rank {rank} -- loop")
    policy_weights.irecv(0)
    frames = 0
    for i, data in enumerate(collector):
        data.isend(dst=0)
        frames += data.numel()
        if (
            frames < total_frames
            and (i + 1) % update_interval == 0
            and not policy_weights.is_empty()
        ):
            policy_weights.irecv(0)

    if not collector.closed:
        collector.shutdown()
    del collector
    return


class DistributedSyncDataCollector(_DataCollector):
    """A distributed synchronous data collector with torch.distributed backend.

    Args:
        create_env_fn (list of callables or EnvBase instances): a list of the
            same length as the number of nodes to be launched.
        policy (Callable[[TensorDict], TensorDict]): a callable that populates
            the tensordict with an `"action"` field.
        frames_per_batch (int): the number of frames to be gathered in each
            batch.
        total_frames (int): the total number of frames to be collected from the
            distributed collector.
        collector_class (type or str, optional): a collector class for the remote node. Can be
            :class:`torchrl.collectors.SyncDataCollector`,
            :class:`torchrl.collectors.MultiSyncDataCollector`,
            :class:`torchrl.collectors.MultiaSyncDataCollector`
            or a derived class of these. The strings "single", "sync" and
            "async" correspond to respective class.
            Defaults to :class:`torchrl.collectors.SyncDataCollector`.
        collector_kwargs (dict or list, optional): a dictionary of parameters to be passed to the
            remote data-collector. If a list is provided, each element will
            correspond to an individual set of keyword arguments for the
            dedicated collector.
        num_workers_per_collector (int, optional): the number of copies of the
            env constructor that is to be used on the remote nodes.
            Defaults to 1 (a single env per collector).
            On a single worker node all the sub-workers will be
            executing the same environment. If different environments need to
            be executed, they should be dispatched across worker nodes, not
            subnodes.
        slurm_kwargs (dict): a dictionary of parameters to be passed to the
            submitit executor.
        backend (str, optional): must a string "<distributed_backed>" where
            <distributed_backed> is one of "gloo", "mpi", "nccl" or "ucc". See
            the torch.distributed documentation for more information.
            Defaults to "gloo".
        update_after_each_batch (bool, optional): if ``True``, the weights will
            be updated after each collection. For ``sync=True``, this means that
            all workers will see their weights updated. For ``sync=False``,
            only the worker from which the data has been gathered will be
            updated.
            Defaults to ``False``, ie. updates have to be executed manually
            through
            ``torchrl.collectors.distributed.DistributedDataCollector.update_policy_weights_()``
        max_weight_update_interval (int, optional): the maximum number of
            batches that can be collected before the policy weights of a worker
            is updated.
            For sync collections, this parameter is overwritten by ``update_after_each_batch``.
            For async collections, it may be that one worker has not seen its
            parameters being updated for a certain time even if ``update_after_each_batch``
            is turned on.
            Defaults to -1 (no forced update).
        update_interval (int, optional): the frequency at which the policy is
            updated. Defaults to 1.
        launcher (str, optional): how jobs should be launched.
            Can be one of "submitit" or "mp" for multiprocessing. The former
            can launch jobs across multiple nodes, whilst the latter will only
            launch jobs on a single machine. "submitit" requires the homonymous
            library to be installed.
            To find more about submitit, visit
            https://github.com/facebookincubator/submitit
            Defaults to "submitit".
        tcp_port (int, optional): the TCP port to be used. Defaults to 10003.
    """

    def __init__(
        self,
        create_env_fn,
        policy,
        frames_per_batch,
        total_frames,
        collector_class=SyncDataCollector,
        collector_kwargs=None,
        num_workers_per_collector=1,
        slurm_kwargs=None,
        backend="gloo",
        storing_device="cpu",
        update_after_each_batch=False,
        max_weight_update_interval=-1,
        update_interval=1,
        launcher="submitit",
        tcp_port=None,
    ):
        if collector_class == "async":
            collector_class = MultiaSyncDataCollector
        elif collector_class == "sync":
            collector_class = MultiSyncDataCollector
        elif collector_class == "single":
            collector_class = SyncDataCollector
        self.collector_class = collector_class
        self.env_constructors = create_env_fn
        self.policy = policy
        if isinstance(policy, nn.Module):
            policy_weights = TensorDict(dict(policy.named_parameters()), [])
            policy_weights = policy_weights.apply(lambda x: x.data)
        else:
            policy_weights = TensorDict({}, [])
        self.policy_weights = policy_weights
        self.num_workers = len(create_env_fn)
        self.frames_per_batch = frames_per_batch
        self.storing_device = storing_device
        # make private to avoid changes from users during collection
        self.update_interval = update_interval
        self.total_frames_per_collector = total_frames // self.num_workers
        if self.total_frames_per_collector * self.num_workers != total_frames:
            raise RuntimeError(
                f"Cannot dispatch {total_frames} frames across {self.num_workers}. "
                f"Consider using a number of frames that is divisible by the number of workers."
            )
        self.update_after_each_batch = update_after_each_batch
        self.max_weight_update_interval = max_weight_update_interval
        self.launcher = launcher
        self._batches_since_weight_update = [0 for _ in range(self.num_workers)]
        if tcp_port is None:
            self.tcp_port = os.environ.get("TCP_PORT", "10003")
        else:
            self.tcp_port = str(tcp_port)

        if self.frames_per_batch % self.num_workers != 0:
            raise RuntimeError(
                f"Cannot dispatch {self.frames_per_batch} frames across {self.num_workers}. "
                f"Consider using a number of frames per batch that is divisible by the number of workers."
            )
        self._frames_per_batch_corrected = self.frames_per_batch // self.num_workers

        self.num_workers_per_collector = num_workers_per_collector
        self.total_frames = total_frames
        self.slurm_kwargs = (
            slurm_kwargs if slurm_kwargs is not None else DEFAULT_SLURM_CONF
        )
        collector_kwargs = collector_kwargs if collector_kwargs is not None else {}
        self.collector_kwargs = (
            collector_kwargs
            if isinstance(collector_kwargs, (list, tuple))
            else [collector_kwargs] * self.num_workers
        )
        self.backend = backend

        # os.environ['TP_SOCKET_IFNAME'] = 'lo'

        self._init_workers()
        self._make_container()

    def _init_master_dist(
        self,
        world_size,
        backend,
    ):
        TCP_PORT = self.tcp_port
        print("init master...", end="\t")
        torch.distributed.init_process_group(
            backend,
            rank=0,
            world_size=world_size,
            timeout=timedelta(MAX_TIME_TO_CONNECT),
            init_method=f"tcp://{self.IPAddr}:{TCP_PORT}",
        )
        print("done")

    def _make_container(self):
        env_constructor = self.env_constructors[0]
        pseudo_collector = SyncDataCollector(
            env_constructor,
            self.policy,
            frames_per_batch=self._frames_per_batch_corrected,
            total_frames=self.total_frames,
            split_trajs=False,
        )
        for _data in pseudo_collector:
            break
        if not issubclass(self.collector_class, SyncDataCollector):
            # Multi-data collectors
            self._tensordict_out = (
                _data.expand((self.num_workers, *_data.shape))
                .to_tensordict()
                .to(self.storing_device)
            )
        else:
            # Multi-data collectors
            self._tensordict_out = (
                _data.expand((self.num_workers, *_data.shape))
                .to_tensordict()
                .to(self.storing_device)
            )
        self._single_tds = self._tensordict_out.unbind(0)
        self._tensordict_out.lock_()
        pseudo_collector.shutdown()
        del pseudo_collector

    def _init_worker_dist_submitit(self, executor, i):
        TCP_PORT = self.tcp_port
        env_make = self.env_constructors[i]
        if not isinstance(env_make, (EnvBase, EnvCreator)):
            env_make = CloudpickleWrapper(env_make)
        job = executor.submit(
            _distributed_init_collection_node,
            i + 1,
            self.IPAddr,
            int(TCP_PORT),
            self.num_workers + 1,
            self.backend,
            self.collector_class,
            self.num_workers_per_collector,
            env_make,
            self.policy,
            self._frames_per_batch_corrected,
            self.collector_kwargs[i],
            self.update_interval,
            self.total_frames_per_collector,
        )
        return job

    def _init_worker_dist_mp(self, i):
        TCP_PORT = self.tcp_port
        env_make = self.env_constructors[i]
        if not isinstance(env_make, (EnvBase, EnvCreator)):
            env_make = CloudpickleWrapper(env_make)
        job = mp.Process(
            target=_distributed_init_collection_node,
            args=(
                i + 1,
                self.IPAddr,
                int(TCP_PORT),
                self.num_workers + 1,
                self.backend,
                self.collector_class,
                self.num_workers_per_collector,
                env_make,
                self.policy,
                self._frames_per_batch_corrected,
                self.collector_kwargs[i],
                self.update_interval,
                self.total_frames_per_collector,
            ),
        )
        job.start()
        return job

    def _init_workers(self):

        hostname = socket.gethostname()
        IPAddr = socket.gethostbyname(hostname)
        print("Server IP address:", IPAddr)
        self.IPAddr = IPAddr
        os.environ["MASTER_ADDR"] = str(self.IPAddr)
        os.environ["MASTER_PORT"] = str(self.tcp_port)

        self.jobs = []
        if self.launcher == "submitit":
            if not _has_submitit:
                raise ImportError("submitit not found.") from SUBMITIT_ERR
            executor = submitit.AutoExecutor(folder="log_test")
            executor.update_parameters(**self.slurm_kwargs)
        for i in range(self.num_workers):
            print("Submitting job")
            if self.launcher == "submitit":
                job = self._init_worker_dist_submitit(
                    executor,
                    i,
                )
                print("job id", job.job_id)  # ID of your job
            elif self.launcher == "mp":
                job = self._init_worker_dist_mp(
                    i,
                )
                print("job launched")
            self.jobs.append(job)
        self._init_master_dist(self.num_workers + 1, self.backend)

    def iterator(self):
        yield from self._iterator_dist()

    def _iterator_dist(self):

        total_frames = 0
        j = -1
        while total_frames < self.total_frames:
            j += 1
            if j % self.update_interval == 0:
                for i in range(self.num_workers):
                    rank = i + 1
                    self.policy_weights.isend(rank)

            trackers = []
            for i in range(self.num_workers):
                rank = i + 1
                trackers.append(
                    self._single_tds[i].irecv(src=rank, return_premature=True)
                )
            for tracker in trackers:
                for _tracker in tracker:
                    _tracker.wait()

            data = self._tensordict_out.clone()
            total_frames += data.numel()
            yield data

    def update_policy_weights_(self, worker_rank=None) -> None:
        raise NotImplementedError

    def set_seed(self, seed: int, static_seed: bool = False) -> int:
        raise NotImplementedError

    def state_dict(self) -> OrderedDict:
        raise NotImplementedError

    def load_state_dict(self, state_dict: OrderedDict) -> None:
        raise NotImplementedError

    def shutdown(self):
        pass
