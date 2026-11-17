# ArduPilot SITL (ArduCopter, generic quad), headless, MAVLink on TCP.
#
#   docker compose -f sitl/compose.yaml up --build
#
# The first build clones and compiles ArduPilot (about 10-20 minutes and 2 GB
# of build cache); later starts take seconds. The final image only keeps the
# arducopter binary and its default parameters.

FROM ubuntu:24.04 AS build
ARG ARDUPILOT_REF=Copter-4.6.3
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates git g++ make python3 python3-venv pkg-config \
    && rm -rf /var/lib/apt/lists/*
# waf needs these Python modules. empy must be 3.x and setuptools must still
# ship pkg_resources (removed in 81) for this ArduPilot version.
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir "empy==3.3.4" future pexpect "setuptools<81"
ENV PATH=/opt/venv/bin:$PATH
RUN git clone --depth 1 --branch ${ARDUPILOT_REF} https://github.com/ArduPilot/ardupilot.git /ardupilot \
    && cd /ardupilot && git submodule update --init --recursive --depth 1
WORKDIR /ardupilot
RUN ./waf configure --board sitl && ./waf copter

FROM ubuntu:24.04
COPY --from=build /ardupilot/build/sitl/bin/arducopter /usr/local/bin/arducopter
COPY --from=build /ardupilot/Tools/autotest/default_params/copter.parm /sitl/copter.parm
COPY planner.parm run-sitl.sh /sitl/
# Backstop for checkouts made before .gitattributes: strip Windows CRLF line endings.
RUN sed -i 's/\r$//' /sitl/run-sitl.sh /sitl/planner.parm
WORKDIR /sitl/data
# 5760: the planner backend. 5762 and 5763: a ground control station or MAVProxy.
EXPOSE 5760 5762 5763
ENTRYPOINT ["/bin/sh", "/sitl/run-sitl.sh"]
