#!/usr/bin/env python2

import math
import numpy as np
from scipy import signal
from matplotlib import pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from timeit import default_timer as time

import rospy
from std_msgs.msg import Header
from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint
from nav_msgs.msg import Odometry


class TrajectoryGeneration:
    def __init__(self, node_name='trajectory_gen_node', subscriber='mavros/local_position/odom', publisher='mavros/JointTrajectory'):

        rospy.init_node(node_name, anonymous=True)

        # Define suscribers & publishers
        rospy.Subscriber(subscriber, Odometry, self.callback)
        self.pub = rospy.Publisher(publisher, JointTrajectory, queue_size=10)

        # Define & initialize private variables
        self.YAW_HEADING = ['auto', [1, 0]]  # options: ['auto'], ['center', [x, y]], ['axes', [x, y]]
        self.TRAJECTORY_REQUESTED_SPEED = 1.5  # req. trajectory linear speed [m.s-1] (used when arg velocity in not specify in discretise_trajectory())
        self.LANDING_SPEED = 0.3  # [m.s-1]
        self.PUBLISH_RATE = 10  # publisher frequency [Hz]
        self.FREQUENCY = 100  # point trajectory frequency [Hz]
        self.BOX_LIMIT = [[-2., 2.], [-1., 3.], [-.01, 2.]]  # [[x_min, x_max], [y_min, y_max], [z_min, z_max]]
        self.WINDOW_FRAME = .5  # publish future states comprise in the window time frame [s]

        self.MAX_LINEAR_ACC_XY = 2.5  # max. linear acceleration [m.s-2]
        self.MAX_LINEAR_ACC_Z = 3.0  # max. linear acceleration [m.s-2]

        self.MAX_LINEAR_SPEED_XY = 10.0  # max. linear speed [m.s-1] (only used by generate_states_filtered(), not by generate_states_sg_filtered())
        self.MAX_LINEAR_SPEED_Z = 12.0  # max. linear speed [m.s-1] (only used by generate_states_filtered(), not by generate_states_sg_filtered())

        # Define & initialize flags
        self.is_filtered = False
        self.is_first_callback = False

    def discretise_trajectory(self, parameters=[], velocity=False, heading=False):
        # Trajectory definition - shape/vertices in inertial frame (x, y, z - up)
        #
        # Define trajectory by using:
        # trajectory_object.discretise_trajectory(parameters=['name', param], (opt.) velocity=float, (opt.) heading=options)
        #
        # Possible parameters:
        # parameters=['takeoff', z] with z in meters
        # parameters=['hover', time] with time in seconds
        # parameters=['vector', [x, y, z]] with x, y, z the target position
        # parameters=['circle', [x, y, z], (opt.) n] with x, y, z the center of the circle and n (optional) the number of circle. Circle
        # is defined by the drone position when starting the circle trajectory and the center. The drone will turn around this point.
        # parameters=['landing']
        # parameters=['returnhome']
        #
        # Optional argument:
        # velocity=float
        # heading=options with options: ['auto'], ['still'], ['center', [x, y]], ['axes', [x, y]]

        start = time()

        if not hasattr(self, 'x_discretized'):
            self.x_discretized = [.0] * self.FREQUENCY
            self.y_discretized = [.0] * self.FREQUENCY
            self.z_discretized = [.0] * self.FREQUENCY

        if not hasattr(self, 'ya_info'):
            self.ya_info = self.YAW_HEADING * self.FREQUENCY

        if not velocity:
            velocity = self.TRAJECTORY_REQUESTED_SPEED

        if not heading:
            heading = self.YAW_HEADING

        x1 = self.x_discretized[-1]
        y1 = self.y_discretized[-1]
        z1 = self.z_discretized[-1]

        v0 = np.array([x1, y1, z1])

        if parameters[0] == 'takeoff':
            profil = self.get_linear_position_profil(abs(parameters[1] - z1), velocity, self.MAX_LINEAR_ACC_Z, self.FREQUENCY)

            sign = math.copysign(1.0, parameters[1] - z1)

            x = x1 * np.ones(len(profil))
            y = y1 * np.ones(len(profil))
            z = [z1 + sign * l for l in profil]

        elif parameters[0] == 'hover':
            steps = int(parameters[1] * self.FREQUENCY)

            x = x1 * np.ones(steps)
            y = y1 * np.ones(steps)
            z = z1 * np.ones(steps)

        elif parameters[0] == 'vector':
            vf = np.array(parameters[1])
            vector = vf - v0
            d = np.linalg.norm(vector)
            if (d == 0):
                return
            vector_u = vector / d

            profil = self.get_linear_position_profil(d, velocity, self.MAX_LINEAR_ACC_XY, self.FREQUENCY)

            x = [x1 + l * vector_u[0] for l in profil]
            y = [y1 + l * vector_u[1] for l in profil]
            z = [z1 + l * vector_u[2] for l in profil]

        elif parameters[0] == 'circle':
            n = parameters[2] if len(parameters) > 2 else 1
            center = np.array(parameters[1])
            r = np.linalg.norm(center - v0)
            circumference = 2 * math.pi * r

            cos_a = (x1 - center[0]) / r
            sin_a = (y1 - center[1]) / r

            profil = self.get_linear_position_profil(n * circumference, velocity, self.MAX_LINEAR_ACC_XY / math.sqrt(2), self.FREQUENCY)

            x = [(cos_a*math.cos(l/r) - sin_a*math.sin(l/r)) * r + center[0] for l in profil]
            y = [(sin_a*math.cos(l/r) + cos_a*math.sin(l/r)) * r + center[1] for l in profil]
            z = z1 * np.ones(len(profil))

        elif parameters[0] == 'landing':
            profil = self.get_linear_position_profil(abs(z1), self.LANDING_SPEED, self.MAX_LINEAR_ACC_Z, self.FREQUENCY)

            x = x1 * np.ones(len(profil))
            y = y1 * np.ones(len(profil))
            z = [z1 - l for l in profil]

        elif parameters[0] == 'returnhome':
            vf = np.array([self.x_discretized[0], self.y_discretized[0], z1])
            vector = vf - v0
            d = np.linalg.norm(vector)
            if (d == 0):
                return
            vector_u = vector / d

            profil1 = self.get_linear_position_profil(d, velocity, self.MAX_LINEAR_ACC_XY, self.FREQUENCY)

            x = [x1 + l * vector_u[0] for l in profil1]
            y = [y1 + l * vector_u[1] for l in profil1]
            z = z1 * np.ones(len(profil1))

            profil2 = self.get_linear_position_profil(abs(z1 - self.z_discretized[0]), self.LANDING_SPEED, self.MAX_LINEAR_ACC_Z, self.FREQUENCY)

            x = np.concatenate((x, x[-1] * np.ones(len(profil2))), axis=None)
            y = np.concatenate((y, y[-1] * np.ones(len(profil2))), axis=None)
            z = np.concatenate((z, [z1 - l for l in profil2]), axis=None)

        # elif parameters[0] == 'square':
        # elif parameters[0] == 'inf':

        self.x_discretized.extend(x[1:])
        self.y_discretized.extend(y[1:])
        self.z_discretized.extend(z[1:])

        self.ya_info.extend([heading] * len(x[1:]))

        print('discretise_trajectory() - {} runs in {} s'.format(parameters[0], time() - start))

    def get_linear_position_profil(self, distance, vmax, amax, freq):
        # compute a saturated triangular velocity based profil and return the discretization of the distance
        if (distance == 0):
            return [0.]

        dt = 1./freq

        tf = self.get_final_time_for_profil(distance, vmax, amax)

        dv = self.get_linear_velocity_profil(vmax, amax, tf, dt)

        return [x for x in self.xfintegrate(dv, dt)]

    def get_final_time_for_profil(self, distance, vmax, amax):

        # compute final time tf from the area which represents the distance and the acceleration
        tf = math.sqrt(4 * distance / amax)

        # compute the height of the original triangle
        h = tf * amax / 2.

        h1 = vmax
        if (h > h1):
            t_m = vmax / amax  # time to reach vmax
            t2 = tf - 2 * t_m
            area2 = t2 * (h - h1) / 2
            tf += area2 / h1

        return tf

    def get_linear_velocity_profil(self, vmax, amax, tf, dt):

        tf_2 = tf / 2.
        v_tf_2 = tf_2 * amax

        return [min(amax * t if (t < tf_2) else v_tf_2 - amax * (t - tf_2), vmax) for t in self.xfrange(tf, dt)]

    def xfrange(self, end, step):
        x = .0
        while x < end:
            yield x
            x += step

    def xfintegrate(self, l, dt):
        x = .0
        for element in l:
            x += element * dt
            yield x

    def constraint_trajectory_to_box(self):

        self.x_discretized = [self.BOX_LIMIT[0][0] if x < self.BOX_LIMIT[0][0] else x for x in self.x_discretized]
        self.x_discretized = [self.BOX_LIMIT[0][1] if x > self.BOX_LIMIT[0][1] else x for x in self.x_discretized]
        self.y_discretized = [self.BOX_LIMIT[1][0] if x < self.BOX_LIMIT[1][0] else x for x in self.y_discretized]
        self.y_discretized = [self.BOX_LIMIT[1][1] if x > self.BOX_LIMIT[1][1] else x for x in self.y_discretized]
        self.z_discretized = [self.BOX_LIMIT[2][0] if x < self.BOX_LIMIT[2][0] else x for x in self.z_discretized]
        self.z_discretized = [self.BOX_LIMIT[2][1] if x > self.BOX_LIMIT[2][1] else x for x in self.z_discretized]

    def generate_states(self):

        start = time()

        self.x_discretized.extend([self.x_discretized[-1]] * self.FREQUENCY)
        self.y_discretized.extend([self.y_discretized[-1]] * self.FREQUENCY)
        self.z_discretized.extend([self.z_discretized[-1]] * self.FREQUENCY)

        self.ya_info.extend(['still'] * self.FREQUENCY)

        self.ya_discretized = [.0]
        self.vx_discretized = [.0]
        self.vy_discretized = [.0]
        self.vz_discretized = [.0]
        self.ax_discretized = [.0]
        self.ay_discretized = [.0]
        self.az_discretized = [.0]
        self.ti_discretized = [.0]

        prevHeading = np.array([.0, .0])

        for s, _ in enumerate(self.x_discretized[1:]):
            p1 = np.array([self.x_discretized[s], self.y_discretized[s]])
            p2 = np.array([self.x_discretized[s+1], self.y_discretized[s+1]])

            if self.ya_info[s][0] == 'center':
                heading = np.array(self.ya_info[s][1]) - p1
            elif self.ya_info[s][0] == 'axes':
                heading = np.array(self.ya_info[s][1])
            elif self.ya_info[s][0] == 'still':
                heading = prevHeading
            else:
                heading = p2 - p1

            if (np.linalg.norm(heading) < 0.001) or (self.ya_info[s][0] == 'still'):
                heading = prevHeading
            else:
                heading = heading / np.linalg.norm(heading)
                prevHeading = heading

            self.ya_discretized.append(math.atan2(heading[1], heading[0]))
            self.vx_discretized.append((self.x_discretized[s+1] - self.x_discretized[s]) * self.FREQUENCY)
            self.vy_discretized.append((self.y_discretized[s+1] - self.y_discretized[s]) * self.FREQUENCY)
            self.vz_discretized.append((self.z_discretized[s+1] - self.z_discretized[s]) * self.FREQUENCY)
            self.ax_discretized.append((self.vx_discretized[-1] - self.vx_discretized[-2]) * self.FREQUENCY)
            self.ay_discretized.append((self.vy_discretized[-1] - self.vy_discretized[-2]) * self.FREQUENCY)
            self.az_discretized.append((self.vz_discretized[-1] - self.vz_discretized[-2]) * self.FREQUENCY)
            self.ti_discretized.append((s + 1.) / self.FREQUENCY)

        print('generate_states() runs in {} s'.format(time() - start))

    def generate_states_filtered(self):

        start = time()

        self.x_filtered = [self.x_discretized[0]]
        self.y_filtered = [self.y_discretized[0]]
        self.z_filtered = [self.z_discretized[0]]

        self.vx_filtered = [.0]
        self.vy_filtered = [.0]
        self.vz_filtered = [.0]

        self.ax_filtered = [.0]
        self.ay_filtered = [.0]
        self.az_filtered = [.0]

        for s, _ in enumerate(self.vx_discretized[1:]):
            self.ax_filtered.append(self.saturate((self.vx_discretized[s+1] - self.vx_filtered[-1]) * self.FREQUENCY, self.MAX_LINEAR_ACC_XY))
            self.ay_filtered.append(self.saturate((self.vy_discretized[s+1] - self.vy_filtered[-1]) * self.FREQUENCY, self.MAX_LINEAR_ACC_XY))
            self.az_filtered.append(self.saturate((self.vz_discretized[s+1] - self.vz_filtered[-1]) * self.FREQUENCY, self.MAX_LINEAR_ACC_Z))

            self.vx_filtered.append(self.saturate(self.vx_filtered[-1] + (self.ax_filtered[-1] / self.FREQUENCY), self.MAX_LINEAR_SPEED_XY))
            self.vy_filtered.append(self.saturate(self.vy_filtered[-1] + (self.ay_filtered[-1] / self.FREQUENCY), self.MAX_LINEAR_SPEED_XY))
            self.vz_filtered.append(self.saturate(self.vz_filtered[-1] + (self.az_filtered[-1] / self.FREQUENCY), self.MAX_LINEAR_SPEED_Z))

            self.x_filtered.append(self.x_filtered[-1] + (self.vx_filtered[-1] / self.FREQUENCY))
            self.y_filtered.append(self.y_filtered[-1] + (self.vy_filtered[-1] / self.FREQUENCY))
            self.z_filtered.append(self.z_filtered[-1] + (self.vz_filtered[-1] / self.FREQUENCY))

        self.is_filtered = True

        print('generate_states_filtered() runs in {} s'.format(time() - start))

    def generate_states_sg_filtered(self, window_length=51, polyorder=3, deriv=0, delta=1.0, mode='mirror', on_filtered=False):
        # Info: Apply Savitzky-Golay filter to velocities
        start = time()

        self.x_filtered = [self.x_discretized[0]]
        self.y_filtered = [self.y_discretized[0]]
        self.z_filtered = [self.z_discretized[0]]

        if on_filtered:
            self.vx_filtered = signal.savgol_filter(x=self.vx_filtered, window_length=window_length, polyorder=polyorder, deriv=deriv, delta=delta, mode=mode)
            self.vy_filtered = signal.savgol_filter(x=self.vy_filtered, window_length=window_length, polyorder=polyorder, deriv=deriv, delta=delta, mode=mode)
            self.vz_filtered = signal.savgol_filter(x=self.vz_filtered, window_length=window_length, polyorder=polyorder, deriv=deriv, delta=delta, mode=mode)
        else:
            self.vx_filtered = signal.savgol_filter(x=self.vx_discretized, window_length=window_length, polyorder=polyorder, deriv=deriv, delta=delta, mode=mode)
            self.vy_filtered = signal.savgol_filter(x=self.vy_discretized, window_length=window_length, polyorder=polyorder, deriv=deriv, delta=delta, mode=mode)
            self.vz_filtered = signal.savgol_filter(x=self.vz_discretized, window_length=window_length, polyorder=polyorder, deriv=deriv, delta=delta, mode=mode)

        self.ax_filtered = [.0]
        self.ay_filtered = [.0]
        self.az_filtered = [.0]

        for s, _ in enumerate(self.vx_filtered[1:]):
            self.ax_filtered.append(self.saturate((self.vx_filtered[s+1] - self.vx_filtered[s]) * self.FREQUENCY, self.MAX_LINEAR_ACC_XY))
            self.ay_filtered.append(self.saturate((self.vy_filtered[s+1] - self.vy_filtered[s]) * self.FREQUENCY, self.MAX_LINEAR_ACC_XY))
            self.az_filtered.append(self.saturate((self.vz_filtered[s+1] - self.vz_filtered[s]) * self.FREQUENCY, self.MAX_LINEAR_ACC_Z))

            self.vx_filtered[s+1] = self.vx_filtered[s] + (self.ax_filtered[-1] / self.FREQUENCY)
            self.vy_filtered[s+1] = self.vy_filtered[s] + (self.ay_filtered[-1] / self.FREQUENCY)
            self.vz_filtered[s+1] = self.vz_filtered[s] + (self.az_filtered[-1] / self.FREQUENCY)

            self.x_filtered.append(self.x_filtered[-1] + (self.vx_filtered[s+1] / self.FREQUENCY))
            self.y_filtered.append(self.y_filtered[-1] + (self.vy_filtered[s+1] / self.FREQUENCY))
            self.z_filtered.append(self.z_filtered[-1] + (self.vz_filtered[s+1] / self.FREQUENCY))

        self.is_filtered = True

        print('generate_states_sg_filtered() runs in {} s'.format(time() - start))

    def generate_yaw_filtered(self, is_1st_order=False):

        if not self.is_filtered:
            return

        start = time()

        self.ya_filtered = []

        prevHeading = np.array([.0, .0, .0])

        for s, _ in enumerate(self.vx_filtered[1:]):
            p1 = np.array([self.x_filtered[s], self.y_filtered[s]])
            p2 = np.array([self.x_filtered[s+1], self.y_filtered[s+1]])

            if self.ya_info[s][0] == 'center':
                heading = np.array(self.ya_info[s][1]) - p1
            elif self.ya_info[s][0] == 'axes':
                heading = np.array(self.ya_info[s][1])
            elif self.ya_info[s][0] == 'still':
                heading = prevHeading
            else:
                heading = p2 - p1

            if (np.linalg.norm(heading) < 0.001) or (self.ya_info[s][0] == 'still'):
                heading = prevHeading
            else:
                heading = heading / np.linalg.norm(heading)
                prevHeading = heading

            self.ya_filtered.append(math.atan2(heading[1], heading[0]))

        self.ya_filtered.append(self.ya_filtered[-1])

        cos_ya = [math.cos(yaw) for yaw in self.ya_filtered]
        sin_ya = [math.sin(yaw) for yaw in self.ya_filtered]

        cos_ya = signal.savgol_filter(x=cos_ya, window_length=53, polyorder=1, deriv=0, delta=1.0, mode='mirror')
        sin_ya = signal.savgol_filter(x=sin_ya, window_length=53, polyorder=1, deriv=0, delta=1.0, mode='mirror')

        self.ya_filtered = []

        for s, _ in enumerate(cos_ya):
            self.ya_filtered.append(math.atan2(sin_ya[s], cos_ya[s]))

        print('generate_yaw_filtered() runs in {} s'.format(time() - start))

    def plot_trajectory_extras(self):

        start = time()

        n = self.FREQUENCY / self.PUBLISH_RATE
        alpha = .3  # Transparancy for velocity and heading arrows

        ti = self.ti_filtered if hasattr(self, 'ti_filtered') else self.ti_discretized

        fig = plt.figure(figsize=(16, 8))

        ax1 = fig.add_subplot(121, projection='3d')
        ax1.scatter(self.x_discretized[0::n], self.y_discretized[0::n], self.z_discretized[0::n], label='trajectory_desired', color='blue')
        if self.is_filtered:
            ax1.scatter(self.x_filtered, self.y_filtered, self.z_filtered, label='trajectory_filtered', color='red')
            ax1.quiver(
                self.x_filtered[0::n], self.y_filtered[0::n], self.z_filtered[0::n],
                self.vx_filtered[0::n], self.vy_filtered[0::n], self.vz_filtered[0::n],
                length=.05, color='red', alpha=alpha, label='velocity_filtered')
            if hasattr(self, 'ya_filtered'):
                ax1.quiver(
                    self.x_filtered[0::n], self.y_filtered[0::n], self.z_filtered[0::n],
                    [math.cos(a) for a in self.ya_filtered[0::n]], [math.sin(a) for a in self.ya_filtered[0::n]], [.0 for a in self.ya_filtered[0::n]],
                    length=.3, color='green', alpha=alpha, label='heading_filtered')
            else:
                ax1.quiver(
                    self.x_filtered[0::n], self.y_filtered[0::n], self.z_filtered[0::n],
                    [math.cos(a) for a in self.ya_discretized[0::n]], [math.sin(a) for a in self.ya_discretized[0::n]], [.0 for a in self.ya_discretized[0::n]],
                    length=.3, color='green', alpha=alpha, label='heading_discretized')
        else:
            ax1.quiver(
                self.x_discretized[0::n], self.y_discretized[0::n], self.z_discretized[0::n],
                self.vx_discretized[0::n], self.vy_discretized[0::n], self.vz_discretized[0::n],
                length=.05, color='red', alpha=alpha, label='velocity')
            ax1.quiver(
                self.x_discretized[0::n], self.y_discretized[0::n], self.z_discretized[0::n],
                [math.cos(a) for a in self.ya_discretized[0::n]], [math.sin(a) for a in self.ya_discretized[0::n]], [.0 for a in self.ya_discretized[0::n]],
                length=.3, color='green', alpha=alpha, label='heading')
        plt.legend()
        plt.title('Trajectory')

        ax2 = fig.add_subplot(322)
        ax2.plot(self.ti_discretized, self.vx_discretized, color='red', label='vx_desired')
        ax2.plot(self.ti_discretized, self.vy_discretized, color='green', label='vy_desired')
        ax2.plot(self.ti_discretized, self.vz_discretized, color='blue', label='vz_desired')
        if self.is_filtered:
            ax2.plot(ti, self.vx_filtered, color='red', label='vx_filtered', linestyle='--')
            ax2.plot(ti, self.vy_filtered, color='green', label='vy_filtered', linestyle='--')
            ax2.plot(ti, self.vz_filtered, color='blue', label='vz_filtered', linestyle='--')
        plt.legend()
        plt.title('Velocity')

        ax3 = fig.add_subplot(324)
        ax3.plot(self.ti_discretized, self.ax_discretized, color='red', label='ax_desired')
        ax3.plot(self.ti_discretized, self.ay_discretized, color='green', label='ay_desired')
        ax3.plot(self.ti_discretized, self.az_discretized, color='blue', label='az_desired')
        if self.is_filtered:
            ax3.plot(ti, self.ax_filtered, color='red', label='ax_filtered', linestyle='--')
            ax3.plot(ti, self.ay_filtered, color='green', label='ay_filtered', linestyle='--')
            ax3.plot(ti, self.az_filtered, color='blue', label='az_filtered', linestyle='--')
        ax3.set_ylim([-max(self.MAX_LINEAR_ACC_XY, self.MAX_LINEAR_ACC_Z), max(self.MAX_LINEAR_ACC_XY, self.MAX_LINEAR_ACC_Z)])
        plt.legend()
        plt.title('Acceleration')

        ax4 = fig.add_subplot(326)
        ax4.plot(self.ti_discretized, self.ya_discretized, color='blue', marker='o', markersize='1.', linestyle='None', label='ya_desired')
        if hasattr(self, 'ya_filtered'):
            ax4.plot(ti, self.ya_filtered, color='red', marker='o', markersize='1.', linestyle='None', label='ya_filtered')
        plt.legend()
        plt.title('Yaw')

        print('plot_trajectory_extras_filtered() runs in {} s'.format(time() - start))

        fig.tight_layout()
        plt.show()

    def start(self):

        rate = rospy.Rate(self.PUBLISH_RATE)
        ratio = int(self.FREQUENCY / self.PUBLISH_RATE)
        window_points = self.WINDOW_FRAME * self.FREQUENCY
        string_id = str(rospy.get_rostime().nsecs)
        s = 0

        x = self.x_filtered if hasattr(self, 'x_filtered') else self.x_discretized
        y = self.y_filtered if hasattr(self, 'y_filtered') else self.y_discretized
        z = self.z_filtered if hasattr(self, 'z_filtered') else self.z_discretized
        ya = self.ya_filtered if hasattr(self, 'ya_filtered') else self.ya_discretized
        vx = self.vx_filtered if hasattr(self, 'vx_filtered') else self.vx_discretized
        vy = self.vy_filtered if hasattr(self, 'vy_filtered') else self.vy_discretized
        vz = self.vz_filtered if hasattr(self, 'vz_filtered') else self.vz_discretized
        ax = self.ax_filtered if hasattr(self, 'ax_filtered') else self.ax_discretized
        ay = self.ay_filtered if hasattr(self, 'ay_filtered') else self.ay_discretized
        az = self.az_filtered if hasattr(self, 'az_filtered') else self.az_discretized
        ti = self.ti_filtered if hasattr(self, 'ti_filtered') else self.ti_discretized

        while not (rospy.is_shutdown() or s >= len(x)-window_points):
            # Build JointTrajectory message
            header = Header()
            header.seq = s
            header.stamp = rospy.get_rostime()
            header.frame_id = string_id

            joint_trajectory_msg = JointTrajectory()
            joint_trajectory_msg.header = header
            joint_trajectory_msg.joint_names = ['t', 't1']

            points_in_next_trajectory = int(min(window_points, len(x)-s))

            for i in range(points_in_next_trajectory):
                joint_trajectory_point = JointTrajectoryPoint()
                joint_trajectory_point.positions = [x[s+i], y[s+i], z[s+i], ya[s+i]]
                joint_trajectory_point.velocities = [vx[s+i], vy[s+i], vz[s+i]]
                joint_trajectory_point.accelerations = [ax[s+i], ay[s+i], az[s+i]]
                joint_trajectory_point.effort = []
                joint_trajectory_point.time_from_start = rospy.Duration.from_sec(ti[s+i])

                joint_trajectory_msg.points.append(joint_trajectory_point)

            s += ratio

            self.pub.publish(joint_trajectory_msg)
            rate.sleep()

    def saturate(self, x, y):

        return math.copysign(min(x, y, key=abs), x)

    def callback(self, odom):

        if not self.is_first_callback:

            position = odom.pose.pose.position

            self.x_discretized = [position.x] * self.FREQUENCY
            self.y_discretized = [position.y] * self.FREQUENCY
            self.z_discretized = [position.z] * self.FREQUENCY

            self.is_first_callback = True

    def check_callback(self):

        rospy.loginfo("Waiting for position measurement callback ...")
        while not (rospy.is_shutdown() or self.is_first_callback):
            pass
        rospy.loginfo("Position measurement callback ok.")


if __name__ == '__main__':

    node_name = 'trajectory_gen_node'
    subscriber = 'mavros/local_position/odom'
    publisher = 'mavros/JointTrajectory'

    try:
        trajectory_object = TrajectoryGeneration(node_name=node_name, subscriber=subscriber, publisher=publisher)

        # Wait for the first measurement callback to initialize the starting position of the trajectory
        trajectory_object.check_callback()

        ########################################################################
        # Configuration
        trajectory_object.YAW_HEADING = ['auto', [1, 0]]  # options: ['auto'], ['still'], ['center', [x, y]], ['axes', [x, y]]

        trajectory_object.TRAJECTORY_REQUESTED_SPEED = 0.4  # req. trajectory linear speed [m.s-1] (used when arg velocity in not specify in discretise_trajectory())
        trajectory_object.LANDING_SPEED = 0.3  # [m.s-1]

        trajectory_object.MAX_LINEAR_ACC_XY = 6.0  # max. linear acceleration [m.s-2]
        trajectory_object.MAX_LINEAR_ACC_Z = 5.0  # max. linear acceleration [m.s-2]
        ########################################################################

        ########################################################################
        # Trajectory definition - shape/vertices in inertial frame (x, y, z - up)
        #
        # Define trajectory by using:
        # trajectory_object.discretise_trajectory(parameters=['name', param], (opt. arg) velocity=float, (opt. arg) heading=[] (see YAW_HEADING))
        #
        # Possible parameters:
        # parameters=['takeoff', z] with z in meters
        # parameters=['hover', time] with time in seconds
        # parameters=['vector', [x, y, z]] with x, y, z the target position
        # parameters=['circle', [x, y, z], (opt.) n] with x, y, z the center of the circle and n (optional) the number of circle. Circle
        # is defined by the drone position when starting the circle trajectory and the center. The drone will turn around this point.
        # parameters=['landing']

        # Takeoff trajectory example:
        # trajectory_object.discretise_trajectory(parameters=['takeoff', 1.], velocity=0.6)
        # trajectory_object.discretise_trajectory(parameters=['hover', 10.], heading=['still'])
        # trajectory_object.discretise_trajectory(parameters=['landing'])

        # Square trajectory example:
        # trajectory_object.discretise_trajectory(parameters=['takeoff', 1.], velocity=0.6, heading=['axes', [1, 0]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 20.], heading=['still'])
        # trajectory_object.discretise_trajectory(parameters=['vector', [1., -.5, 1.]], heading=['axes', [1, 0]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.], heading=['still'])
        # trajectory_object.discretise_trajectory(parameters=['vector', [1., 1.5, 1.]], heading=['auto', [1, 0]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.], heading=['still'])
        # trajectory_object.discretise_trajectory(parameters=['vector', [-1., 1.5, 1.]], heading=['auto', [1, 0]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.], heading=['still'])
        # trajectory_object.discretise_trajectory(parameters=['vector', [-1., -.5, 1.]], heading=['auto', [1, 0]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.], heading=['still'])
        # trajectory_object.discretise_trajectory(parameters=['vector', [0., -.5, 1.]], heading=['auto', [1, 0]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.], heading=['still'])
        # trajectory_object.discretise_trajectory(parameters=['landing'])

        # Circle trajectory example:
        trajectory_object.discretise_trajectory(parameters=['takeoff', 1.], velocity=0.6)
        trajectory_object.discretise_trajectory(parameters=['hover', 5.], heading=['still'])
        trajectory_object.discretise_trajectory(parameters=['vector', [0., -.3, 1.]], velocity=0.6, heading=['axes', [1, 0]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 5.], heading=['still'])
        # trajectory_object.discretise_trajectory(parameters=['circle', [.0, .5, 1.], 2], velocity=0.6, heading=['axes', [1, 0]])
        trajectory_object.discretise_trajectory(parameters=['hover', 5.], heading=['still'])
        trajectory_object.discretise_trajectory(parameters=['circle', [.0, .5, 1.], 1], velocity=1.0, heading=['auto', [1, 0]])
        trajectory_object.discretise_trajectory(parameters=['circle', [.0, .5, 1.], 1], velocity=1.1, heading=['auto', [1, 0]])
        trajectory_object.discretise_trajectory(parameters=['circle', [.0, .5, 1.], 1], velocity=1.2, heading=['auto', [1, 0]])
        trajectory_object.discretise_trajectory(parameters=['circle', [.0, .5, 1.], 1], velocity=1.3, heading=['auto', [1, 0]])
        trajectory_object.discretise_trajectory(parameters=['circle', [.0, .5, 1.], 1], velocity=1.4, heading=['auto', [1, 0]])
        trajectory_object.discretise_trajectory(parameters=['circle', [.0, .5, 1.], 1], velocity=1.5, heading=['auto', [1, 0]])
        trajectory_object.discretise_trajectory(parameters=['circle', [.0, .5, 1.], 1], velocity=1.6, heading=['auto', [1, 0]])
        trajectory_object.discretise_trajectory(parameters=['circle', [.0, .5, 1.], 1], velocity=1.7, heading=['auto', [1, 0]])
        trajectory_object.discretise_trajectory(parameters=['hover', 5.])
        trajectory_object.discretise_trajectory(parameters=['landing'])

        # More complex circle trajectory example:
        # trajectory_object.discretise_trajectory(parameters=['takeoff', 2.], velocity=1.0)
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.])
        # trajectory_object.discretise_trajectory(parameters=['circle', [.0, 2., 2.], 2], velocity=0.6)
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.])
        # trajectory_object.discretise_trajectory(parameters=['circle', [.0, 2., 2.], 2], velocity=1.2, heading=['auto'])
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.])
        # trajectory_object.discretise_trajectory(parameters=['circle', [.0, 2., 2.], 2], velocity=1.5, heading=['axes', [1, 0]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.])
        # trajectory_object.discretise_trajectory(parameters=['circle', [.0, 2., 2.], 2], velocity=1.5, heading=['axes', [0, 1]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.], heading=['still'])
        # trajectory_object.discretise_trajectory(parameters=['circle', [.0, 2., 2.], 2], velocity=0.6, heading=['center', [0, 2]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.], heading=['still'])
        # trajectory_object.discretise_trajectory(parameters=['circle', [.0, 2., 2.], 2], velocity=1.0, heading=['still'])
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.], heading=['still'])
        # trajectory_object.discretise_trajectory(parameters=['returnhome'], velocity=1.0, heading=['axes', [1, 0]])

        # More complex trajectory example:
        # trajectory_object.discretise_trajectory(parameters=['takeoff', 2.], velocity=1.0)
        # trajectory_object.discretise_trajectory(parameters=['hover', 3.])
        # trajectory_object.discretise_trajectory(parameters=['circle', [.0, 30., 2.], 2], velocity=10, heading=['auto'])
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.])
        # trajectory_object.discretise_trajectory(parameters=['vector', [1., 2., 3.]], velocity=1.0)
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.])
        # trajectory_object.discretise_trajectory(parameters=['circle', [.0, 1., 3.], 2], velocity=10, heading=['auto'])
        # trajectory_object.discretise_trajectory(parameters=['hover', 2.])
        # trajectory_object.discretise_trajectory(parameters=['returnhome'], velocity=0.4, heading=['axes', [1, 0]])

        # HCERES demonstration trajectory (january 2020):
        # trajectory_object.discretise_trajectory(parameters=['takeoff', 1.0])
        # trajectory_object.discretise_trajectory(parameters=['hover', 5.])
        # trajectory_object.discretise_trajectory(parameters=['vector', [1., -0.8, 1.]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 3.])
        # trajectory_object.discretise_trajectory(parameters=['vector', [1., 0.7, 1.]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 3.])
        # trajectory_object.discretise_trajectory(parameters=['vector', [-1.04, -0.55, 1.]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 3.])
        # trajectory_object.discretise_trajectory(parameters=['circle', [.0, .0, 1.]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 3.])
        # trajectory_object.discretise_trajectory(parameters=['vector', [0., -.9, 1.]])
        # trajectory_object.discretise_trajectory(parameters=['hover', 3.])
        # trajectory_object.discretise_trajectory(parameters=['landing'])
        ########################################################################

        # Limit the trajectory to the BOX_LIMIT
        # trajectory_object.constraint_trajectory_to_box()

        # Generate the list of states - start by generating the states and then filter them
        trajectory_object.generate_states()
        trajectory_object.generate_states_sg_filtered(window_length=53, polyorder=1, mode='mirror')
        trajectory_object.generate_states_sg_filtered(window_length=13, polyorder=1, mode='mirror', on_filtered=True)
        trajectory_object.generate_yaw_filtered()

        # Plot the trajectory
        trajectory_object.plot_trajectory_extras()

        # Publish trajectory states
        trajectory_object.start()

    except rospy.ROSInterruptException:
        pass
