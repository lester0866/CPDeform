import numpy as np
import cv2
import taichi as ti
import matplotlib.pyplot as plt

# TODO: run on GPU, fast_math will cause error on float64's sqrt; removing it cuases compile error..
ti.init(arch=ti.gpu, debug=False, fast_math=True)


@ti.data_oriented
class TaichiEnv:
    def __init__(self, cfg, nn=False, loss=True):
        """
        A taichi env builds scene according the configuration and the set of manipulators
        """
        # primitives are environment specific parameters ..
        # move it inside can improve speed; don't know why..
        from .mpm_simulator import MPMSimulator
        from .primitive import Primitives
        from .renderer import Renderer
        from .shapes import Shapes
        from .losses import Loss
        from .nn.mlp import MLP

        self.cfg = cfg.ENV
        self.primitives = Primitives(cfg.PRIMITIVES)
        self.shapes = Shapes(cfg.SHAPES)
        self.init_particles, self.particle_colors, self.particle_obj_ids = self.shapes.get()

        cfg.SIMULATOR.defrost()
        cfg.RENDERER.defrost()
        self.n_particles = cfg.SIMULATOR.n_particles = cfg.RENDERER.max_num_particles = len(self.init_particles)

        self.simulator = MPMSimulator(cfg.SIMULATOR, self.primitives)
        self.renderer = Renderer(cfg.RENDERER, self.primitives)

        if nn:
            self.nn = MLP(self.simulator, self.primitives, (256, 256))

        if loss:
            self.loss = Loss(cfg.ENV.loss, self.simulator)
        else:
            self.loss = None
        self._is_copy = True

    def set_copy(self, is_copy: bool):
        self._is_copy = is_copy

    def initialize(self):
        # initialize all taichi variable according to configurations..
        self.primitives.initialize()
        self.simulator.initialize()
        self.renderer.initialize()
        if self.loss:
            self.loss.initialize()
            self.renderer.set_target_density(self.loss.target_density.to_numpy() / self.simulator.p_mass)

        # call set_state instead of reset..
        self.simulator.reset(self.init_particles)
        self.simulator.particle_obj_id.from_numpy(self.particle_obj_ids)
        if self.loss:
            if self.cfg.loss.target_path: self.loss.reset()
            self.loss.clear()

    def render(self, mode='human', **kwargs):
        assert self._is_copy, "The environment must be in the copy mode for render ..."
        if self.n_particles > 0:
            x = self.simulator.get_x(0)
            obj_id = self.simulator.get_obj_id()
            self.renderer.set_particles(x, obj_id, self.particle_colors)
        img = self.renderer.render_frame(**kwargs)

        if img.shape[-1] >= 3:
            img[:, :, :3] = img[:, :, :3].clip(0, 1) * 255
        if img.shape[-1] == 3:
            img = np.uint8(img)

        if mode == 'human':
            cv2.imshow('x', img[..., ::-1])
            cv2.waitKey(1)
        elif mode == 'plt':
            import matplotlib.pyplot as plt
            plt.imshow(img)
            plt.show()
        else:
            return img

    def step(self, action=None):
        if action is not None:
            action = np.array(action)
        self.simulator.step(is_copy=self._is_copy, action=action)

    def compute_loss(self):
        assert self.loss is not None
        if self._is_copy:
            self.loss.clear()
            return self.loss.compute_loss(0)
        else:
            return self.loss.compute_loss(self.simulator.cur)

    def get_state(self):
        assert self.simulator.cur == 0
        return {
            'state': self.simulator.get_state(0),
            'softness': self.primitives.get_softness(),
            'is_copy': self._is_copy
        }

    def set_state(self, state, softness, is_copy):
        self.simulator.cur = 0
        self.simulator.set_state(0, state)
        self.primitives.set_softness(softness)
        self._is_copy = is_copy
        if self.loss:
            # self.loss.reset()
            self.loss.clear()

    def set_gmass_target_from_particles(self, p):
        state = self.get_state()
        self.simulator.reset(p)

        self.simulator.grid_m.fill(0)
        self.simulator.compute_grid_m_kernel(0)
        gmass = self.simulator.grid_m.to_numpy()
        self.loss.load_target_density(grids=gmass)
        self.renderer.set_target_density(self.loss.target_density.to_numpy() /
                                         self.simulator.p_mass)
        self.set_state(**state)
        self.loss.reset()