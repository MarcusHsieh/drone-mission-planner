#!/bin/sh
# Start ArduCopter SITL at the dock. Settings come from the environment so the
# same image serves the cloud test (SPEEDUP=10) and the live demo (SPEEDUP=1).
#
#   HOME_LOC  lat,lon,alt_m_amsl,heading_deg   (default: the dock at hole 1)
#   SPEEDUP   simulation speed vs real time    (default: 1)
#   MODEL     SITL frame                       (default: quad, "+" layout)
#   PARM      comma-separated parameter files  (default: copter.parm, planner.parm)
set -e
HOME_LOC="${HOME_LOC:-37.733897,-121.929056,140,0}"
SPEEDUP="${SPEEDUP:-1}"
MODEL="${MODEL:-quad}"
PARM="${PARM:-/sitl/copter.parm,/sitl/planner.parm}"
echo "ArduCopter SITL: model=$MODEL home=$HOME_LOC speedup=$SPEEDUP"
echo "MAVLink: tcp 5760 (planner), 5762 and 5763 (ground station)"
# --serial0 tcp:0 (no :wait) so SITL starts flying its clock before anyone connects.
# -w wipes saved parameters so every start is the same clean vehicle.
exec arducopter -w --model "$MODEL" --speedup "$SPEEDUP" --home "$HOME_LOC" \
    --defaults "$PARM" --sim-address 127.0.0.1 -I0 \
    --serial0 tcp:0 --serial1 tcp:2 --serial2 tcp:3
