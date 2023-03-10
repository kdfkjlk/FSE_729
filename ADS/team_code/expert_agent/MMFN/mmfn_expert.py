#!/usr/bin/env python

# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.

"""
This module provides a MMFN agent to control the ego vehicle
"""

import carla
from lib.basic_agent import BasicAgent
from leaderboard.autoagents.autonomous_agent import AutonomousAgent, Track
from srunner.scenariomanager.carla_data_provider import CarlaDataProvider
from lib.misc import get_speed
from munch import DefaultMunch
import os
import lmdb
import datetime
import numpy as np
from utils import bcolors as bc
import yaml

def get_entry_point():
    return 'MMFN_Agent'

class MMFN_Agent(AutonomousAgent):

    def set_global_plan(self, global_plan_gps, global_plan_world_coord):
        self.origin_global_plan_world_coord = [(global_plan_world_coord[x][0], global_plan_world_coord[x][1]) for x in range(len(global_plan_world_coord))]
        super().set_global_plan(global_plan_gps, global_plan_world_coord)

    def setup(self, config):
        self.track = Track.MAP
        self._route_assigned = False
        self._agent = None
        self.num_frames = 0
        self.stop_counter = 0
        with open(config, "r") as stream:
            try:
                self.config = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                print(exc)
                return None
        self.config = DefaultMunch.fromDict(self.config)
        if self.config.save_data: # force False for save data
            self.config.debug_print = False
        self.rgbs, self.sems, self.info = [], [], []

    def sensors(self):
        camera_w = 1024
        camera_h = 288
        fov = 100
        sensors = [
            {'type': 'sensor.camera.rgb', 'x': 1.5, 'y': 0.0, 'z': 2.4, 'roll': 0.0, 'pitch': 0.0, 'yaw': 0.0,
             'width': camera_w, 'height': camera_h, 'fov': fov, 'id': 'RGB'},
            {'type': 'sensor.camera.semantic_segmentation', 'x': 1.5, 'y': 0.0, 'z': 2.4,  'roll': 0.0, 'pitch': 0.0, 'yaw': 0.0,
            'width': camera_w, 'height': camera_h, 'fov': fov, 'id': 'SEM'},
            {'type': 'sensor.speedometer', 'id': 'EGO'},
        ]
        return sensors

    def run_step(self, input_data, timestamp):
        """
        Execute one step of navigation.
        """
        control = carla.VehicleControl(steer=0, throttle=0, brake=1)

        if not self._agent:
            self.init_set_routes()
            return control  

        control, obs_actor, light_actor, walker = self._agent.run_step()

        _, rgb = input_data.get('RGB')
        _, sem = input_data.get('SEM')
        _, ego = input_data.get('EGO')
        spd = ego.get('speed')

        if spd < 0.5:
            self.stop_counter += 1
        else:
            self.stop_counter = 0

        if self.num_frames % 5 == 0 and self.stop_counter < self.config.max_stop_num and self.config.save_data:
            vel = get_speed(self._vehicle)/3.6 #  m/s
            is_junction = self._map.get_waypoint(self._vehicle.get_transform().location).is_junction
            self.rgbs.append(rgb[...,:3])
            self.sems.append(sem[...,2,])
            self.info.append([vel, is_junction, self.config.weather_change])

            # change weather
            if not self.config.debug_print and self.num_frames % 50 == 0:
                self.change_weather()

            if len(self.rgbs)>self.config.num_per_flush:
                self.flush_data()

        self.num_frames += 1
        if self.stop_counter>self.config.counter_destory and self.config.force_destory_actor:
            self.force_destory_actor(obs_actor, light_actor, walker)

        return control

    def force_destory_actor(self, obs, light, walker):
        if obs:
            self._world.get_actor(obs.id).destroy()
            self.stop_counter = 0
            print(f"{self.num_frames}, {bc.WARNING}ATTENTION:{bc.ENDC} force to detroy actor {obs.id} stopping for a long time")
        elif walker:
            self._world.get_actor(walker.id).destroy()
            self.stop_counter = 0
            print(f"{self.num_frames}, {bc.WARNING}ATTENTION:{bc.ENDC} force to detroy actor {walker.id} stopping for a long time")
        # only for there are some problems with light
        # elif light and self.stop_counter>self.config.counter_destory*2:
        #     light.set_green_time(10.0)
        #     light.set_state(carla.TrafficLightState.Green)
        #     self.stop_counter = 0
        #     print(f"{self.num_frames}, {bc.WARNING}ATTENTION:{bc.ENDC} force to setting green light {light.id}")
        else:
            print(f"{bc.WARNING}==========> warnning!!!! {bc.ENDC} None factor trigger the stop!!!")
            return

    def init_set_routes(self):
        self._vehicle = CarlaDataProvider.get_hero_actor()
        self._world = self._vehicle.get_world()
        self._map = self._world.get_map()
        self._agent = BasicAgent(self._vehicle, self.config, interpolate=False, debug=self.config.debug_print)
        plan = []

        for transform, _ in self.origin_global_plan_world_coord:
            wp = CarlaDataProvider.get_map().get_waypoint(transform.location)
            plan.append(wp.transform.location)

        self._agent.set_global_plan(plan)

        if self.config.debug_print:
            for i, point in enumerate(plan):
                self._world.debug.draw_string(point, str(i), life_time=999, color=carla.Color(0,0,255))

    def change_weather(self):
        # TODO
        return

    def flush_data(self):
        # Save data
        now = datetime.datetime.now()
        folder_name = f'rid_{self.config.route_id:02d}_'
        time_now = '_'.join(map(lambda x: '%02d' % x, (now.month, now.day, now.hour, now.minute, now.second)))
        data_path = os.path.join(self.config.data_save, folder_name+time_now)

        if not os.path.exists(data_path):
            os.makedirs(data_path)
            print ('======> Saving to {}'.format(data_path))

        lmdb_env = lmdb.open(data_path, map_size=int(1e10))
        db_length = len(self.info)

        with lmdb_env.begin(write=True) as txn:
            txn.put('len'.encode(), str(db_length).encode())
            for i in range(db_length):
                txn.put(
                    f'info_{i:05d}'.encode(),
                    np.ascontiguousarray(self.info[i]).astype(np.float32),
                )
                txn.put(
                    f'rgbs_{i:05d}'.encode(),
                    np.ascontiguousarray(self.rgbs[i]).astype(np.uint8)
                )

                txn.put(
                    f'sems_{i:05d}'.encode(),
                    np.ascontiguousarray(self.sems[i]).astype(np.uint8)
                )

        self.rgbs.clear()
        self.sems.clear()
        self.info.clear()
        lmdb_env.close()

        return

    def destroy(self):
        if len(self.rgbs) == 0:
            return

        self.flush_data()
        
