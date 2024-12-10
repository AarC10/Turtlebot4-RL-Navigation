import gymnasium as gym
import numpy as np
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import TwistStamped
from nav_msgs.msg import Odometry
from sensor_msgs.msg import LaserScan
from std_srvs.srv import Trigger

from geometry_msgs.msg import Pose, Twist, Quaternion
from gz.transport14 import Node
from gz.msgs11.pose_pb2 import Pose
from gz.msgs11.boolean_pb2 import Boolean
import time
import math
import tf_transformations  # For quaternion to euler conversions


class TurtleBotNavEnv(gym.Env):
    def __init__(self, start_position, goal_position, is_discrete, max_wait_for_observation=5.0):
        super().__init__()

        if not rclpy.ok():
            rclpy.init(args=None)

        self.node = rclpy.create_node('turtlebot_nav_env')
        self.is_discrete = is_discrete

        # Define action spaces
        if self.is_discrete:
            # 4 Discrete Actions (forward, backwards, left, right) vs Continuous
            self.action_space = gym.spaces.Discrete(4)
        else:
            # Bounds for moving [linear, angular]
            self.action_space = gym.spaces.Box(low=np.array([-3.0, -1.5]), high=np.array([3.0, 1.5]), dtype=np.float32)

        # Continuous observation (LiDAR scans)
        self.observation_space = gym.spaces.Box(
            low=0.0, high=10.0, shape=(640,), dtype=np.float32
        )

        # Pub/Sub
        self.cmd_vel_pub = self.node.create_publisher(TwistStamped, '/cmd_vel', 10)
        self.scan_sub = self.node.create_subscription(LaserScan, '/scan', self.scan_callback, 10)
        self.odom_sub = self.node.create_subscription(Odometry, '/odom', self.odom_callback, 10)

        # State
        self.state = None
        self.start_position = np.array(start_position, dtype=np.float32)
        self.goal_position = np.array(goal_position, dtype=np.float32)
        self.current_position = np.copy(self.start_position)
        self.current_yaw = 0.0  # Current orientation (yaw)
        self.last_distance_to_goal = np.linalg.norm(self.goal_position - self.current_position)
        self.best_distance_to_goal = np.linalg.norm(self.goal_position - self.start_position)
        self.steps_since_improvement = 0
        self.steps_of_improvement = 0
        self.max_steps_without_improvement = 10000
        self.distance_degradation_limit = 50
        self.done = False
        self.collision = False
        self.max_wait_for_observation = max_wait_for_observation
        self.collision_count = 0

        # Reward Tracking Variables
        self.previous_position = np.copy(self.start_position)
        self.stationary_steps = 0
        self.stationary_threshold = 0.01  # Threshold to consider the robot as stationary
        self.max_stationary_steps = 100  # Maximum allowed stationary steps before penalty

        # Reward Coefficients
        self.alpha = 10000.0  # Weight for distance improvement
        self.beta = 10000.0  # Weight for distance regression
        self.collision_penalty = 100.0
        self.step_penalty = 0.1  # Increased step penalty to discourage taking too long
        self.stationary_penalty = 50.0

        # Odometry offset variables
        self.odom_position_offset = np.array([0.0, 0.0], dtype=np.float32)
        self.odom_orientation_offset = 0.0
        self.odom_prev_yaw = 0.0
        self.odom_calibrated = False

        # Yaw Offset Variable
        self.yaw_offset = 0.0  # Yaw offset to align with desired yaw

        self._reset_robot_position()
        self._print_and_log("TurtleBotNavEnv initialized.")

    def scan_callback(self, msg):
        """Updates state with current scan data."""
        self.state = np.array(msg.ranges, dtype=np.float32)

    def odom_callback(self, msg):
        """Updates current position and orientation, applying odometry offsets if calibrated."""
        # Extract position
        odom_x = msg.pose.pose.position.x
        odom_y = msg.pose.pose.position.y

        # Extract orientation (yaw)
        odom_q = msg.pose.pose.orientation
        odom_euler = tf_transformations.euler_from_quaternion([
            odom_q.x,
            odom_q.y,
            odom_q.z,
            odom_q.w
        ])
        odom_yaw = odom_euler[2]  # Yaw angle in radians

        if not self.odom_calibrated:
            # Calibrate odometry offsets
            self.odom_position_offset = np.array([
                odom_x - self.start_position[0],
                odom_y - self.start_position[1]
            ], dtype=np.float32)

            # Set yaw offset based on desired yaw (facing downwards)
            desired_yaw = -math.pi / 2  # Facing downwards (270 degrees)
            self.yaw_offset = desired_yaw - odom_yaw

            self.odom_calibrated = True
            self._print_and_log(f"Odometry calibrated. Position offset: {self.odom_position_offset}, Orientation offset: {self.yaw_offset:.2f} radians.")

            # Reset current position and yaw to start position and desired yaw (facing downwards)
            self.current_position = np.copy(self.start_position)
            self.current_yaw = desired_yaw
            return

        # Apply position offset
        adjusted_x = odom_x - self.odom_position_offset[0]
        adjusted_y = odom_y - self.odom_position_offset[1]
        self.current_position = np.array([adjusted_x, adjusted_y], dtype=np.float32)

        # Apply orientation offset
        adjusted_yaw = odom_yaw + self.yaw_offset
        # Normalize yaw to [-pi, pi]
        adjusted_yaw = (adjusted_yaw + math.pi) % (2 * math.pi) - math.pi
        self.current_yaw = adjusted_yaw

    def seed(self, seed=0):
        """Set the random seed for reproducibility."""
        super().seed(seed)
        np.random.seed(seed)

    def reset(self, *, seed=None, options=None):
        """Reset the environment."""
        super().reset(seed=seed)

        self.steps_since_improvement = 0
        self.steps_of_improvement = 0
        self.best_distance_to_goal = np.linalg.norm(self.goal_position - self.start_position)

        self._send_stop_command()
        self.done = False
        self.collision = False
        self.collision_count = 0

        # Reset position in Gazebo
        self._reset_robot_position()

        # Reset state variables
        self.state = None
        self.previous_position = np.copy(self.start_position)
        self.last_distance_to_goal = np.linalg.norm(self.goal_position - self.current_position)
        self.stationary_steps = 0

        # Wait for initial observations
        if not self._wait_for_new_state():
            raise RuntimeError("No LiDAR data received after reset timeout.")

        return self._get_state(), {}

    def step(self, action):
        """Execute one step in the environment."""
        # Take the action
        self._take_action(action)

        # Spin until we get a new scan (or timeout)
        if not self._wait_for_new_state():
            raise RuntimeError("No LiDAR data received after step timeout.")

        # Compute reward and check done
        reward = self._calculate_reward()
        done = self._is_done()
        info = {}
        terminated = done
        truncated = False

        self._print_and_log(f"Position: {self.current_position}, Yaw: {self.current_yaw:.2f} rad -> Goal: {self.goal_position} | Reward: {reward}")

        return self._get_state(), reward, terminated, truncated, info

    def _take_action(self, action):
        """Convert the discrete action into a velocity command."""
        msg = TwistStamped()
        msg.header.stamp = self.node.get_clock().now().to_msg()
        msg.header.frame_id = "base_link"

        print(action)

        if self.is_discrete:
            if action == 0:  # Forward
                msg.twist.linear.x = 0.5
            elif action == 1:  # Left
                msg.twist.angular.z = 0.5
            elif action == 2:  # Right
                msg.twist.angular.z = -0.5
            elif action == 3:  # Backwards
                msg.twist.linear.x = -0.5
        else:
            linear, angular = action
            msg.twist.linear.x = float(linear)
            msg.twist.angular.z = float(angular)

        self.cmd_vel_pub.publish(msg)

    def _send_stop_command(self):
        """Send zero velocity to the robot."""
        msg = TwistStamped()
        msg.header.stamp = self.node.get_clock().now().to_msg()
        msg.header.frame_id = "base_link"
        self.cmd_vel_pub.publish(msg)

    def _get_state(self):
        """Return the current state (LiDAR readings)."""
        if self.state is None:
            # If no state available, return zeros to match observation space shape
            return np.zeros(self.observation_space.shape, dtype=np.float32)
        return self.state

    def _calculate_reward(self):
        """
        - Positive reward for moving closer to the goal.
        - Negative reward for moving away from the goal.
        - Large negative reward for collisions.
        - Small negative reward each step to encourage efficiency.
        - Additional penalty if the robot remains stationary for too long.
        """
        distance_to_goal = np.linalg.norm(self.goal_position - self.current_position)
        delta_distance = self.last_distance_to_goal - distance_to_goal

        # Reward for moving closer to the goal
        if delta_distance > 0:
            reward = self.alpha * delta_distance
        else:
            # Penalty for moving away from the goal
            reward = self.beta * delta_distance  # delta_distance is negative here

        # Update best distance and improvement trackers
        if distance_to_goal < self.best_distance_to_goal:
            self.best_distance_to_goal = distance_to_goal
            self.steps_since_improvement = 0
            self.steps_of_improvement += 1
        else:
            self.steps_since_improvement += 1
            self.steps_of_improvement = 0

        # Penalty for collision
        if self._is_collision():
            reward -= self.collision_penalty

        # Step penalty to encourage efficient navigation
        reward -= self.step_penalty

        # Check for stationary
        position_change = np.linalg.norm(self.current_position - self.previous_position)
        if position_change < self.stationary_threshold:
            self.stationary_steps += 1
            if self.stationary_steps > self.max_stationary_steps:
                reward -= self.stationary_penalty
                self.stationary_steps = 0  # Reset after penalizing
                self._print_and_log("Penalty for being stationary.")
        else:
            self.stationary_steps = 0  # Reset if the robot is moving

        # Update previous position and last distance
        self.previous_position = np.copy(self.current_position)
        self.last_distance_to_goal = distance_to_goal

        return reward

    def _is_done(self):
        """
        Episode ends if:
        - The robot collides with an obstacle.
        - The robot reaches the goal within a certain threshold.
        - No improvement in a while.
        - Too many collisions.
        """
        # End if goal reached
        if np.linalg.norm(self.goal_position - self.current_position) < 0.5:
            self._print_and_log("Goal reached!")
            self.done = True
            return True

        # End if no improvement in a while
        if self.steps_since_improvement > self.max_steps_without_improvement:
            self._print_and_log("No improvement in a while.")
            self.done = True
            return True

        # End if too many collisions
        if self.collision_count > 10:
            self._print_and_log("Too many collisions.")
            self.done = True
            return True

        return self.done

    def _is_collision(self):
        """
        Check for collision based on LiDAR minimum range.
        If any reading is below a threshold, consider it a collision.
        """
        collision_threshold = 0.5

        collision = (self.state is not None) and (np.min(self.state) < collision_threshold)
        if not self.collision and collision:
            self.collision_count += 1
            self._print_and_log(f"Total collisions: {self.collision_count}")

        self.collision = collision

        return self.collision

    def _wait_for_new_state(self):
        """
        Spin until a new LiDAR scan is received or timeout.
        Return True if new state is received, False otherwise.
        """
        start_time = time.time()
        initial_state = self.state
        while (self.state is initial_state) and (time.time() - start_time < self.max_wait_for_observation):
            rclpy.spin_once(self.node, timeout_sec=0.1)
        return self.state is not initial_state

    def _reset_robot_position(self):
        """
        Reset the robot's position and synchronize odometry.
        """
        node = Node()
        pose_msg = Pose()
        pose_msg.name = "turtlebot4"

        pose_msg.position.x = float(self.start_position[0])
        pose_msg.position.y = float(self.start_position[1])
        pose_msg.position.z = 0.0

        yaw = -math.pi / 2  # Desired yaw in radians (facing downwards)
        pose_msg.orientation.w = math.cos(yaw / 2.0)
        pose_msg.orientation.x = 0.0
        pose_msg.orientation.y = 0.0
        pose_msg.orientation.z = math.sin(yaw / 2.0)

        service_name = "/world/rl_maze/set_pose"
        timeout_ms = 1000

        try:
            result, response = node.request(service_name, pose_msg, Pose, Boolean, timeout_ms)
            if not response.data:
                raise RuntimeError("Failed to reset the robot position.")
        except Exception as e:
            raise RuntimeError(f"Service call failed: {e}")

        time.sleep(0.1)

        self._calibrate_odom()

    def _calibrate_odom(self):
        """Spinlock until odometry callback received to determine correct offsets to use."""
        self.odom_calibrated = False
        self.odom_position_offset = np.array([0.0, 0.0], dtype=np.float32)
        self.yaw_offset = 0.0

        self._print_and_log("Calibrating odometry offsets...")

        start_time = time.time()
        timeout = 5.0  # seconds

        while not self.odom_calibrated and (time.time() - start_time) < timeout:
            rclpy.spin_once(self.node, timeout_sec=0.1)
            # The odom_callback will set odom_calibrated to True
        if not self.odom_calibrated:
            raise RuntimeError("Odometry calibration timed out.")

    def _print_and_log(self, message):
        self.node.get_logger().info(message)

    def close(self):
        self._send_stop_command()
        self.node.destroy_node()
        rclpy.shutdown()
