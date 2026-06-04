cmake_minimum_required(VERSION 3.13...3.27)
project(guider_gds CXX)

# guiderGDS exposes DaqStampSource — a stamp source backed by the GDS
# daq-sdk. The same module is used in production (real DAQ partition)
# and in development against Gregg's GDS emulator (R5-V13.16+).
#
# Two independent build switches:
#
#   BUILD_MODULE (default ON)  — build the guiderGDS pybind module.
#                                Requires the daq-sdk (CCSDAQ).
#   BUILD_TESTS  (default OFF) — build the GoogleTest unit tests under
#                                tests/. These cover SDK-free code
#                                (stamp_source.hh) and need NO daq-sdk.
#
# This split lets CI run the C++ unit tests before the daq-sdk is
# available in the dev image:
#     cmake -S . -B build -DBUILD_MODULE=OFF -DBUILD_TESTS=ON
#     cmake --build build
#     ctest --test-dir build --output-junit cpp_test_results.xml
#
# A full local build (module + tests), with CCSDAQ pointing at an
# unpacked daq-sdk release containing include/ and x86/lib/:
#     export CCSDAQ=/home/saluser/ts_repos/R5-V13.16
#     cmake -S . -B build -DBUILD_TESTS=ON
#     cmake --build build

option(BUILD_MODULE "Build the guiderGDS pybind module (needs daq-sdk)" ON)
option(BUILD_TESTS  "Build C++ unit tests under tests/ (no daq-sdk)"    OFF)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

if(BUILD_MODULE)
    if(NOT DEFINED ENV{CCSDAQ})
        message(FATAL_ERROR
            "guiderGDS requires the CCSDAQ environment variable to point "
            "at an unpacked daq-sdk release (containing include/ and "
            "x86/lib/). For example: "
            "export CCSDAQ=/home/saluser/ts_repos/R5-V13.16 "
            "(or configure with -DBUILD_MODULE=OFF to build tests only).")
    endif()

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
endif()

if(BUILD_TESTS)
    enable_testing()
    add_subdirectory(tests)
endif()
