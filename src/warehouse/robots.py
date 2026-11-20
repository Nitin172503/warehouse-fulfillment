"""Picking robots: A* path planning, tour building, battery, and a job queue.

One robot takes one pick job (all lines of one order in one warehouse), plans a
nearest-neighbour tour over the pick faces, drives it one cell per tick, picks at
each stop and returns to the dock.
"""

from __future__ import annotations

import heapq
import itertools
import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .layout import Layout
from .models import Allocation, Coord


class NoPathError(RuntimeError):
    pass


def manhattan(a: Coord, b: Coord) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def astar(layout: Layout, start: Coord, goal: Coord) -> list[Coord]:
    """Shortest 4-connected path; excludes `start`, includes `goal`."""
    if start == goal:
        return []
    if not layout.passable(goal):
        raise NoPathError(f"goal {goal} is blocked")
    tie = itertools.count()
    frontier = [(manhattan(start, goal), next(tie), start)]
    came_from: dict[Coord, Coord] = {}
    cost = {start: 0}
    while frontier:
        _, _, cur = heapq.heappop(frontier)
        if cur == goal:
            break
        for nb in layout.neighbors(cur):
            new_cost = cost[cur] + 1
            if new_cost < cost.get(nb, math.inf):
                cost[nb] = new_cost
                came_from[nb] = cur
                heapq.heappush(frontier, (new_cost + manhattan(nb, goal), next(tie), nb))
    else:
        raise NoPathError(f"no path {start} -> {goal}")
    path, node = [], goal
    while node != start:
        path.append(node)
        node = came_from[node]
    return path[::-1]


@dataclass
class PickTask:
    order_id: str
    sku_id: str
    location_id: str
    qty: int
    allocation: Allocation | None = None
    picked: int = 0
    done: bool = False


@dataclass
class PickJob:
    job_id: str
    order_id: str
    warehouse_id: str
    tasks: list[PickTask]
    created_tick: int = 0
    robot_id: str | None = None

    def demand(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.tasks:
            out[t.sku_id] = out.get(t.sku_id, 0) + t.qty
        return out


class RobotState(str, Enum):
    IDLE = "idle"
    MOVING = "moving"
    CHARGING = "charging"


@dataclass
class Leg:
    path: deque[Coord]
    task: PickTask | None = None


@dataclass
class Robot:
    robot_id: str
    pos: Coord
    battery: float = 100.0
    state: RobotState = RobotState.IDLE
    job: PickJob | None = None
    legs: deque[Leg] = field(default_factory=deque)
    distance: int = 0
    busy_ticks: int = 0
    jobs_completed: int = 0


class Fleet:
    MOVE_COST = 0.25      # battery % per cell
    CHARGE_RATE = 5.0     # battery % per tick at the dock
    LOW_BATTERY = 25.0

    def __init__(
        self,
        layout: Layout,
        n_robots: int,
        on_pick: Callable[[PickTask], int],
        on_job_done: Callable[[PickJob], None],
        prefix: str = "R",
    ) -> None:
        self.layout = layout
        self.on_pick = on_pick
        self.on_job_done = on_job_done
        self.robots = [Robot(f"{prefix}{i + 1}", layout.dock) for i in range(n_robots)]
        self.queue: deque[PickJob] = deque()

    # --- queue management -------------------------------------------------
    def submit(self, job: PickJob) -> None:
        self.queue.append(job)

    def remove(self, job: PickJob) -> None:
        self.queue.remove(job)

    def drain_queue(self) -> list[PickJob]:
        jobs = list(self.queue)
        self.queue.clear()
        return jobs

    @property
    def backlog(self) -> int:
        return len(self.queue) + sum(1 for r in self.robots if r.job is not None)

    # --- planning ---------------------------------------------------------
    def plan_tour(self, start: Coord, tasks: list[PickTask]) -> deque[Leg]:
        """Greedy nearest-neighbour tour over pick faces, then back to the dock."""
        legs: deque[Leg] = deque()
        pos, todo = start, list(tasks)
        while todo:
            nxt = min(todo, key=lambda t: manhattan(pos, self.layout.pick_faces[t.location_id]))
            todo.remove(nxt)
            face = self.layout.pick_faces[nxt.location_id]
            legs.append(Leg(deque(astar(self.layout, pos, face)), nxt))
            pos = face
        legs.append(Leg(deque(astar(self.layout, pos, self.layout.dock))))
        return legs

    # --- simulation ---------------------------------------------------------
    def step(self, accept_new: bool = True) -> None:
        for robot in self.robots:
            if robot.state == RobotState.IDLE and robot.job is None:
                if robot.battery < self.LOW_BATTERY:
                    robot.state = RobotState.CHARGING
                elif accept_new and self.queue:
                    job = self.queue.popleft()
                    job.robot_id = robot.robot_id
                    robot.job = job
                    robot.legs = self.plan_tour(robot.pos, job.tasks)
                    robot.state = RobotState.MOVING

            if robot.state == RobotState.CHARGING:
                robot.battery = min(100.0, robot.battery + self.CHARGE_RATE)
                if robot.battery >= 100.0:
                    robot.state = RobotState.IDLE
            elif robot.state == RobotState.MOVING:
                robot.busy_ticks += 1
                self._advance(robot)

    def _advance(self, robot: Robot) -> None:
        leg = robot.legs[0]
        if leg.path:
            robot.pos = leg.path.popleft()
            robot.battery = max(0.0, robot.battery - self.MOVE_COST)
            robot.distance += 1
        if leg.path:
            return
        if leg.task is not None:
            leg.task.picked = self.on_pick(leg.task)
            leg.task.done = True
        robot.legs.popleft()
        if not robot.legs:
            job, robot.job = robot.job, None
            robot.state = RobotState.IDLE
            robot.jobs_completed += 1
            assert job is not None
            self.on_job_done(job)
