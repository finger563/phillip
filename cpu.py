import ssbm
from state import *
import state_manager
import memory_watcher
from menu_manager import *
import os
from pad import *
import time
import fox
import agent
import util
from ctype_util import copy
import RL
from numpy import random
from reward import computeRewards
import movie

default_args = dict(
    path=None,
    tag=None,
    dump = True,
    dump_max = 10,
    # TODO This might not always be accurate.
    dolphin_dir = '~/.local/share/dolphin-emu/',
    self_play = None,
    model="DQN",
    act_every=5,
    experience_time=60,
    zmq=False,
    p1="marth",
    p2="zelda",
    stage="battlefield",
)

class CPU:
    def __init__(self, **kwargs):
        for k, v in default_args.items():
            if k in kwargs and kwargs[k] is not None:
                setattr(self, k, kwargs[k])
            else:
                setattr(self, k, v)

        self.fps = 60 // self.act_every
        self.experience_length = self.experience_time * self.fps
        
        if self.dump:
            self.dump_dir = self.path + "/experience/"
            os.makedirs(self.dump_dir, exist_ok=True)
            self.dump_tag = "" if self.tag is None else str(self.tag) + "-"
            self.dump_size = self.experience_length
            self.dump_state_actions = (self.dump_size * ssbm.SimpleStateAction)()

            self.dump_frame = 0
            self.dump_count = 0

        self.reward_logfile = self.path + '/rewards.log'
        self.first_frame = True
        self.action_counter = 0
        self.toggle = False

        self.dolphin_dir = os.path.expanduser(self.dolphin_dir)

        self.state = ssbm.GameMemory()
        # track players 1 and 2 (pids 0 and 1)
        self.sm = state_manager.StateManager([0, 1])
        self.write_locations(self.dolphin_dir)

        self.fox = fox.Fox()
        if self.tag is not None:
            random.seed(self.tag)
        
        self.cpus = [0, 1] if self.self_play else [1]
        self.agents = []

        reload_every = self.experience_length
        if self.self_play:
            self.enemy = agent.Agent(reload_every=self.self_play*reload_every, swap=True, **kwargs)
            self.agents.append(self.enemy)
        self.agent = agent.Agent(reload_every=reload_every, **kwargs)
        self.agents.append(self.agent)
        
        self.characters = [self.p1, self.p2] if self.self_play else [self.p2]
        self.menu_managers = [MenuManager(characters[c], pid=i) for c, i in zip(self.characters, self.cpus)]

        print('Creating MemoryWatcher.')
        mwType = memory_watcher.MemoryWatcher
        if self.zmq:
          mwType = memory_watcher.MemoryWatcherZMQ
        self.mw = mwType(self.dolphin_dir + '/MemoryWatcher/MemoryWatcher')
        
        pipe_dir = self.dolphin_dir + '/Pipes/'
        print('Creating Pads at %s. Open dolphin now.' % pipe_dir)
        os.makedirs(self.dolphin_dir + '/Pipes/', exist_ok=True)
        
        paths = [pipe_dir + 'phillip%d' % i for i in self.cpus]
        self.get_pads = util.async_map(Pad, paths)

        self.init_stats()
        
        # sets the game mode and random stage
        self.movie = movie.Movie(movie.endless_netplay_battlefield)

    def run(self, frames=None, dolphin_process=None):
        try:
            self.pads = self.get_pads()
        except KeyboardInterrupt:
            print("Pipes not initialized!")
            return

        for mm, pad in zip(self.menu_managers, self.pads):
            mm.pad = pad
        
        self.settings_mm = MenuManager(settings, pid=self.cpus[0], pad=self.pads[0])
        
        print('Starting run loop.')
        self.start_time = time.time()
        try:
            while True:
                self.advance_frame()
        except KeyboardInterrupt:
            if dolphin_process is not None:
                dolphin_process.terminate()
            self.print_stats()

    def init_stats(self):
        self.total_frames = 0
        self.skip_frames = 0
        self.thinking_time = 0

    def print_stats(self):
        total_time = time.time() - self.start_time
        frac_skipped = self.skip_frames / self.total_frames
        frac_thinking = self.thinking_time * 1000 / self.total_frames
        print('Total Time:', total_time)
        print('Total Frames:', self.total_frames)
        print('Average FPS:', self.total_frames / total_time)
        print('Fraction Skipped: {:.6f}'.format(frac_skipped))
        print('Average Thinking Time (ms): {:.6f}'.format(frac_thinking))

    def write_locations(self, dolphin_dir):
        path = dolphin_dir + '/MemoryWatcher/'
        os.makedirs(path, exist_ok=True)
        print('Writing locations to:', path)
        with open(path + 'Locations.txt', 'w') as f:
            f.write('\n'.join(self.sm.locations()))

    def dump_state(self):
        state_action = self.dump_state_actions[self.dump_frame]
        state_action.state = self.state
        state_action.prev = self.agent.prev_action
        state_action.action = self.agent.action

        self.dump_frame += 1

        if self.dump_frame == self.dump_size:
            if self.dump_count == 0:
            # if False:
                dump_path = self.dump_dir + ".dead"
            else:
                dump_path = self.dump_dir + self.dump_tag + str(self.dump_count % self.dump_max)
            print("Dumping to ", dump_path)
            #ssbm.writeStateActions(dump_path, self.dump_state_actions)
            ssbm.writeStateActions_pickle(dump_path, self.dump_state_actions)
            self.dump_count += 1
            self.dump_frame = 0

            rewards = computeRewards(self.dump_state_actions)

            with open(self.reward_logfile, 'a') as f:
                f.write(str(time.time()) + " " + str(sum(rewards) / len(rewards)) + "\n")
                f.flush()

    def advance_frame(self):
        last_frame = self.state.frame
        
        self.update_state()
        if self.state.frame > last_frame:
            skipped_frames = self.state.frame - last_frame - 1
            if skipped_frames > 0:
                self.skip_frames += skipped_frames
                print("Skipped frames ", skipped_frames)
            self.total_frames += self.state.frame - last_frame
            last_frame = self.state.frame

            start = time.time()
            self.make_action()
            self.thinking_time += time.time() - start

            if self.state.frame % (15 * self.fps) == 0:
                self.print_stats()
        
        self.mw.advance()

    def update_state(self):
        messages = self.mw.get_messages()
        for message in messages:
          self.sm.handle(self.state, *message)
    
    def spam(self, button):
        if self.toggle:
            self.pads[0].press_button(button)
            self.toggle = False
        else:
            self.pads[0].release_button(button)
            self.toggle = True
    
    def make_action(self):
        # menu = Menu(self.state.menu)
        # print(menu)
        if self.state.menu == Menu.Game.value:
            if self.action_counter % self.act_every == 0:
                for agent, pad in zip(self.agents, self.pads):
                    agent.act(self.state, pad)
                if self.dump:
                    self.dump_state()
            self.action_counter += 1
            #self.fox.advance(self.state, self.pad)

        elif self.state.menu in [menu.value for menu in [Menu.Characters, Menu.Stages]]:
            # FIXME: this is very convoluted
            done = True
            for mm in self.menu_managers:
                if not mm.reached:
                    done = False
                mm.move(self.state)
            
            if done:
                if self.settings_mm.reached:
                    self.movie.play(self.pads[0])
                else:
                    self.settings_mm.move(self.state)
        elif self.state.menu == Menu.PostGame.value:
            self.spam(Button.START)
        else:
            print("Weird menu state", self.state.menu)

def runCPU(**kwargs):
  CPU(**kwargs).run()

