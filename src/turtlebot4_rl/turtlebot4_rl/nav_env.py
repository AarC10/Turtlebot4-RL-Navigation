import gymnasium as gym
import numpy as np
import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from sensor_msgs.msg import LaserScan
import time

class TurtleBotNavEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(self, start_position, goal_position, max_wait_for_observation=5.0):
        super().__init__()

        if not rclpy.ok():
            rclpy.init(args=None)

        self.node = rclpy.create_node('turtlebot_nav_env')
        
        # Define action and observation spaces
        # 4 Discrete Actions (forward, backwards, left, right)
        self.action_space = gym.spaces.Discrete(4)

        # Continuous observation (LiDAR scans)
        self.observation_space = gym.spaces.Box(
            low=0.0, high=10.0, shape=(640,), dtype=np.float32
        )
        
        # Pub/Sub
        self.cmd_vel_pub = self.node.create_publisher(Twist, '/cmd_vel', 10)
        self.scan_sub = self.node.create_subscription(LaserScan, '/scan', self.scan_callback, 10)
        self.odom_sub = self.node.create_subscription(Odometry, '/odom', self.odom_callback, 10)
        
        # State
        self.state = None
        self.start_position = np.array(start_position, dtype=np.float32)
        self.goal_position = np.array(goal_position, dtype=np.float32)
        self.current_position = np.copy(self.start_position)
        self.last_distance_to_goal = np.linalg.norm(self.goal_position - self.current_position)
        self.done = False
        self.collision = False
        self.max_wait_for_observation = max_wait_for_observation

    def scan_callback(self, msg):
        """Updates state with current scan data."""
        self.state = np.array(msg.ranges, dtype=np.float32)

    def odom_callback(self, msg):
        """Ipdates current position."""
        self.current_position = np.array([
            msg.pose.pose.position.x,
            msg.pose.pose.position.y
        ], dtype=np.float32)

    def seed(self, seed=None):
        """Set the random seed for reproducibility."""
        super().seed(seed)
        np.random.seed(seed)

    def reset(self, *, seed=None, options=None):
        """Reset the environment."""
        super().reset(seed=seed)

        self._send_stop_command()
        self.done = False
        self.collision = False

        # Reset position in Gazebo
        self._reset_robot_position()

        # Reset state
        self.state = None
        self.current_position = np.copy(self.start_position)
        self.last_distance_to_goal = np.linalg.norm(self.goal_position - self.current_position)

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
        terminated = self._is_done()
        truncated = False

        return self._get_state(), reward, terminated, truncated, info

    def _take_action(self, action):
        """Convert the discrete action into a velocity command."""
        msg = Twist()
        if action == 0:  # Forward
            msg.linear.x = 0.5
        elif action == 1:  # Left
            msg.angular.z = 0.5
        elif action == 2:  # Right
            msg.angular.z = -0.5
        elif action == 3:  # Backwards
            msg.linear.x = -0.5

        self.cmd_vel_pub.publish(msg)

    def _send_stop_command(self):
        """Send zero velocity to the robot."""
        msg = Twist()
        self.cmd_vel_pub.publish(msg)

    def _get_state(self):
        """Return the current state (LiDAR readings)."""
        if self.state is None:
            # If no state available, return zeros to match observation space shape
            return np.zeros(self.observation_space.shape, dtype=np.float32)
        return self.state

    def _calculate_reward(self):
        """
        Reward is based on progress towards the goal.
        Moving closer to the goal yields positive reward,
        collisions yield a penalty.
        """
        distance_to_goal = np.linalg.norm(self.goal_position - self.current_position)
        reward = self.last_distance_to_goal - distance_to_goal

        # Penalize collisions
        if self._is_collision():
            reward -= 10.0

        self.last_distance_to_goal = distance_to_goal
        return reward

    def _is_done(self):
        """
        Episode ends if:
        - The robot collides with an obstacle.
        - The robot reaches the goal within a certain threshold.
        """
        if self._is_collision():
            self.done = True
        elif np.linalg.norm(self.goal_position - self.current_position) < 0.5:
            self.done = True
        return self.done

    def _is_collision(self):
        """
        Check for collision based on LiDAR minimum range.
        If any reading is below a threshold, consider it a collision.
        """
        collision_threshold = 0.2  # TODO: Tune
        self.collision = (self.state is not None) and (np.min(self.state) < collision_threshold)
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
        """Reset the robot to the starting position."""
        pass

    def close(self):
        self._send_stop_command()
        self.node.destroy_node()
        rclpy.shutdown()