import time
import numpy as np
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.common.numpy_fast import interp
from openpilot.system.swaglog import cloudlog
from openpilot.selfdrive.controls.lib.lateral_mpc_lib.lat_mpc import LateralMpc
from openpilot.selfdrive.controls.lib.lateral_mpc_lib.lat_mpc import N as LAT_MPC_N
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N, MIN_SPEED, get_speed_error
from openpilot.selfdrive.controls.lib.lane_planner import LanePlanner
from openpilot.selfdrive.controls.lib.desire_helper import DesireHelper
import cereal.messaging as messaging
from cereal import log
from openpilot.selfdrive.hardware import EON

STEERING_CENTER_calibration = []
STEERING_CENTER_calibration_update_count = 0
params = Params()
try:
  with open('../../../handle_center_info.txt','r') as fp:
    handle_center_info_str = fp.read()
    if handle_center_info_str:
      STEERING_CENTER = float(handle_center_info_str)
      with open('/tmp/handle_center_info.txt','w') as fp: #読み出し用にtmpへ書き込み
        fp.write('%0.2f' % (STEERING_CENTER) )
except Exception as e:
  pass

TRAJECTORY_SIZE = 33
if EON:
  CAMERA_OFFSET = -0.06
else:
  CAMERA_OFFSET = 0.04


PATH_COST = 1.0
LATERAL_MOTION_COST = 0.11
LATERAL_ACCEL_COST = 0.0
LATERAL_JERK_COST = 0.04
# Extreme steering rate is unpleasant, even
# when it does not cause bad jerk.
# TODO this cost should be lowered when low
# speed lateral control is stable on all cars
STEERING_RATE_COST = 700.0


class LateralPlanner:
  def __init__(self, CP, debug=False):
    self.DH = DesireHelper()

    # Vehicle model parameters used to calculate lateral movement of car
    self.factor1 = CP.wheelbase - CP.centerToFront
    self.factor2 = (CP.centerToFront * CP.mass) / (CP.wheelbase * CP.tireStiffnessRear)
    self.last_cloudlog_t = 0
    self.solution_invalid_cnt = 0

    self.LP = LanePlanner(True) #widw_camera常にONで呼び出す。
    self.path_xyz = np.zeros((TRAJECTORY_SIZE, 3))
    self.velocity_xyz = np.zeros((TRAJECTORY_SIZE, 3))
    self.plan_yaw = np.zeros((TRAJECTORY_SIZE,))
    self.plan_yaw_rate = np.zeros((TRAJECTORY_SIZE,))
    self.t_idxs = np.arange(TRAJECTORY_SIZE)
    self.y_pts = np.zeros((TRAJECTORY_SIZE,))
    self.v_plan = np.zeros((TRAJECTORY_SIZE,))
    self.v_ego = 0.0
    self.l_lane_change_prob = 0.0
    self.r_lane_change_prob = 0.0

    self.debug_mode = debug

    self.lat_mpc = LateralMpc()
    self.reset_mpc(np.zeros(4))

  def reset_mpc(self, x0=None):
    if x0 is None:
      x0 = np.zeros(4)
    self.x0 = x0
    self.lat_mpc.reset(x0=self.x0)

  def update(self, sm):
    # clip speed , lateral planning is not possible at 0 speed
    measured_curvature = sm['controlsState'].curvature
    v_ego_car = sm['carState'].vEgo

    # Parse model predictions
    md = sm['modelV2']
    self.LP.parse_model(md,v_ego_car) #ichiropilot,lta_mode判定をこの中で行う。
    if len(md.position.x) == TRAJECTORY_SIZE and len(md.orientation.x) == TRAJECTORY_SIZE:
      self.path_xyz = np.column_stack([md.position.x, md.position.y, md.position.z])
      self.t_idxs = np.array(md.position.t)
      self.plan_yaw = np.array(md.orientation.z)
      self.plan_yaw_rate = np.array(md.orientationRate.z)
      self.velocity_xyz = np.column_stack([md.velocity.x, md.velocity.y, md.velocity.z])
      car_speed = np.linalg.norm(self.velocity_xyz, axis=1) - get_speed_error(md, v_ego_car)
      self.v_plan = np.clip(car_speed, MIN_SPEED, np.inf)
      self.v_ego = self.v_plan[0]

    STEER_CTRL_Y = sm['carState'].steeringAngleDeg
    path_y = self.path_xyz[:,1]
    max_yp = 0
    for yp in path_y:
      max_yp = yp if abs(yp) > abs(max_yp) else max_yp
    STEERING_CENTER_calibration_max = 300 #3秒
    if abs(max_yp) / 2.5 < 0.1 and self.v_ego > 20/3.6 and abs(STEER_CTRL_Y) < 8:
      STEERING_CENTER_calibration.append(STEER_CTRL_Y)
      if len(STEERING_CENTER_calibration) > STEERING_CENTER_calibration_max:
        STEERING_CENTER_calibration.pop(0)
    if len(STEERING_CENTER_calibration) > 0:
      value_STEERING_CENTER_calibration = sum(STEERING_CENTER_calibration) / len(STEERING_CENTER_calibration)
    else:
      value_STEERING_CENTER_calibration = 0
    handle_center = 0 #STEERING_CENTER
    global STEERING_CENTER_calibration_update_count
    STEERING_CENTER_calibration_update_count += 1
    if len(STEERING_CENTER_calibration) >= STEERING_CENTER_calibration_max:
      handle_center = value_STEERING_CENTER_calibration #動的に求めたハンドルセンターを使う。
      if STEERING_CENTER_calibration_update_count % 100 == 0:
        with open('../../../handle_center_info.txt','w') as fp: #保存用に間引いて書き込み
          fp.write('%0.2f' % (value_STEERING_CENTER_calibration) )
      if STEERING_CENTER_calibration_update_count % 10 == 5:
        with open('/tmp/handle_center_info.txt','w') as fp: #読み出し用にtmpへ書き込み
          fp.write('%0.2f' % (value_STEERING_CENTER_calibration) )
    else:
      with open('../../../handle_calibct_info.txt','w') as fp:
        fp.write('%d' % ((len(STEERING_CENTER_calibration)+2) / (STEERING_CENTER_calibration_max / 100)) )
    #with open('/tmp/debug_out_y','w') as fp:
    #  path_y_sum = -sum(path_y)
    #  #fp.write('{0}\n'.format(['%0.2f' % i for i in self.path_xyz[:,1]]))
    #  fp.write('calibration:%0.2f/%d ; max:%0.2f ; sum:%0.2f ; avg:%0.2f' % (value_STEERING_CENTER_calibration,len(STEERING_CENTER_calibration),-max_yp , path_y_sum, path_y_sum / len(path_y)) )
    
    # STEER_CTRL_Y -= handle_center #STEER_CTRL_Yにhandle_centerを込みにする。
    # ypf = STEER_CTRL_Y
    # if abs(STEER_CTRL_Y) < abs(max_yp) / 2.5:
    #   STEER_CTRL_Y = (-max_yp / 2.5)

    # if sm['carState'].leftBlinker == True:
    #   STEER_CTRL_Y = 90
    # if sm['carState'].rightBlinker == True:
    #   STEER_CTRL_Y = -90

    # Lane change logic
    desire_state = md.meta.desireState
    if len(desire_state):
      self.l_lane_change_prob = desire_state[log.LateralPlan.Desire.laneChangeLeft]
      self.r_lane_change_prob = desire_state[log.LateralPlan.Desire.laneChangeRight]
    lane_change_prob = self.l_lane_change_prob + self.r_lane_change_prob
    self.DH.update(sm['carState'], sm['carControl'].latActive, lane_change_prob)

    self.lat_mpc.set_weights(PATH_COST, LATERAL_MOTION_COST,
                             LATERAL_ACCEL_COST, LATERAL_JERK_COST,
                             STEERING_RATE_COST)
    
    if self.LP.lta_mode and self.DH.lane_change_state == 0: #LTA有効なら。ただしレーンチェンジ中は発動しない。
      ypf = STEER_CTRL_Y
      STEER_CTRL_Y -= handle_center #STEER_CTRL_Yにhandle_centerを込みにする。
      d_path_xyz = self.LP.get_d_path(STEER_CTRL_Y , (-max_yp / 2.5) , ypf , self.v_ego, self.t_idxs, self.path_xyz)
      y_pts = d_path_xyz[:LAT_MPC_N+1, 1]
    else:
      y_pts = self.path_xyz[:LAT_MPC_N+1, 1]

    heading_pts = self.plan_yaw[:LAT_MPC_N+1]
    yaw_rate_pts = self.plan_yaw_rate[:LAT_MPC_N+1]
    self.y_pts = y_pts

    assert len(y_pts) == LAT_MPC_N + 1
    assert len(heading_pts) == LAT_MPC_N + 1
    assert len(yaw_rate_pts) == LAT_MPC_N + 1
    lateral_factor = np.clip(self.factor1 - (self.factor2 * self.v_plan**2), 0.0, np.inf)
    p = np.column_stack([self.v_plan, lateral_factor])
    self.lat_mpc.run(self.x0,
                     p,
                     y_pts,
                     heading_pts,
                     yaw_rate_pts)
    # init state for next iteration
    # mpc.u_sol is the desired second derivative of psi given x0 curv state.
    # with x0[3] = measured_yaw_rate, this would be the actual desired yaw rate.
    # instead, interpolate x_sol so that x0[3] is the desired yaw rate for lat_control.
    self.x0[3] = interp(DT_MDL, self.t_idxs[:LAT_MPC_N + 1], self.lat_mpc.x_sol[:, 3])

    #  Check for infeasible MPC solution
    mpc_nans = np.isnan(self.lat_mpc.x_sol[:, 3]).any()
    t = time.monotonic()
    if mpc_nans or self.lat_mpc.solution_status != 0:
      self.reset_mpc()
      self.x0[3] = measured_curvature * self.v_ego
      if t > self.last_cloudlog_t + 5.0:
        self.last_cloudlog_t = t
        cloudlog.warning("Lateral mpc - nan: True")

    if self.lat_mpc.cost > 1e6 or mpc_nans:
      self.solution_invalid_cnt += 1
    else:
      self.solution_invalid_cnt = 0

  def publish(self, sm, pm):
    plan_solution_valid = self.solution_invalid_cnt < 2
    plan_send = messaging.new_message('lateralPlan')
    plan_send.valid = sm.all_checks(service_list=['carState', 'controlsState', 'modelV2'])

    lateralPlan = plan_send.lateralPlan
    lateralPlan.modelMonoTime = sm.logMonoTime['modelV2']
    lateralPlan.dPathPoints = self.y_pts.tolist()
    lateralPlan.psis = self.lat_mpc.x_sol[0:CONTROL_N, 2].tolist()

    lateralPlan.curvatures = (self.lat_mpc.x_sol[0:CONTROL_N, 3]/self.v_ego).tolist()
    lateralPlan.curvatureRates = [float(x.item() / self.v_ego) for x in self.lat_mpc.u_sol[0:CONTROL_N - 1]] + [0.0]

    lateralPlan.mpcSolutionValid = bool(plan_solution_valid)
    lateralPlan.solverExecutionTime = self.lat_mpc.solve_time
    if self.debug_mode:
      lateralPlan.solverCost = self.lat_mpc.cost
      lateralPlan.solverState = log.LateralPlan.SolverState.new_message()
      lateralPlan.solverState.x = self.lat_mpc.x_sol.tolist()
      lateralPlan.solverState.u = self.lat_mpc.u_sol.flatten().tolist()

    lateralPlan.desire = self.DH.desire
    lateralPlan.useLaneLines = False
    lateralPlan.laneChangeState = self.DH.lane_change_state
    lateralPlan.laneChangeDirection = self.DH.lane_change_direction

    pm.send('lateralPlan', plan_send)
