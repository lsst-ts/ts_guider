cmake_minimum_required(VERSION 3.13...3.27)
project(guider_gds CXX)

# guiderGDS exposes DaqStampSource — a stamp source backed by the GDS
# daq-sdk. The same module is used in production (real DAQ partition)
# and in development against Gregg's GDS emulator (R5-V13.16+);
#
# The SDK is a build-time prerequisite. Point CCSDAQ at an
# unpacked daq-sdk release that contains include/ and x86/lib/:
# E.g. From a docker container with image: lsstts/develop-env:develop
#     export CCSDAQ=/home/saluser/ts_repos/R5-V13.16

if(NOT DEFINED ENV{CCSDAQ})
    message(FATAL_ERROR
        "guiderGDS requires the CCSDAQ environment variable to point "
        "at an unpacked daq-sdk release (containing include/ and "
        "x86/lib/). For example: "
        "export CCSDAQ=/home/saluser/ts_repos/R5-V13.16")
endif()

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

set(CCSDAQ_DIR $ENV{CCSDAQ})

if(NOT EXISTS "${CCSDAQ_DIR}/include/gds/Decoder.hh")
    message(FATAL_ERROR
        "CCSDAQ=${CCSDAQ_DIR} does not look like a daq-sdk release "
        "(missing include/gds/Decoder.hh).")
endif()

find_package(pybind11 CONFIG REQUIRED)

pybind11_add_module(guiderGDS
    src/pybind_GDS.cpp
    src/Decoder.cc
    src/daq_stamp_source.cc
)

target_include_directories(guiderGDS
    PRIVATE
        src
        ${CCSDAQ_DIR}/include
)

target_link_directories(guiderGDS
    PRIVATE
        ${CCSDAQ_DIR}/x86/lib
)

target_link_libraries(guiderGDS
    PRIVATE
        daq_gds daq_dsi daq_dsm daq_dsid daq_daq daq_dvi
        daq_net daq_osa daq_rms daq_reb daq_scs daq_hash
)

message(STATUS "Building guiderGDS against daq-sdk at ${CCSDAQ_DIR}")
