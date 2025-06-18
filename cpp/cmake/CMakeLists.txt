cmake_minimum_required(VERSION 3.4...3.18)
project(guider)

set(CMAKE_CXX_FLAGS "-I/home/dmills/lsst/guider/R5-V13.5/include")
set(CMAKE_SHARED_LINKER_FLAGS "-L/home/dmills/lsst/guider/R5-V13.5/x86/lib -ldaq_gds -ldaq_dsi -ldaq_dsm -ldaq_dsid -ldaq_daq -ldaq_dvi -ldaq_net -ldaq_osa -ldaq_rms -ldaq_reb -ldaq_scs -ldaq_hash")
set(CMAKE_MODULE_LINKER_FLAGS "-L/home/dmills/lsst/guider/R5-V13.5/x86/lib -l daq_gds -ldaq_dsi -ldaq_dsm -ldaq_dsid -ldaq_daq -ldaq_dvi -ldaq_net -ldaq_osa -ldaq_rms -ldaq_reb -ldaq_scs -ldaq_hash")
add_library(guiderAlgorithms SHARED 
    src/centroidAlgorithms.c
    src/chisqLib.c
    src/gauss.c
    src/guider_calc.c
)
add_library(guiderAlgorithms::library ALIAS guiderAlgorithms)

target_include_directories(guiderAlgorithms
    PUBLIC 
        src
)

add_subdirectory(pybind11)
pybind11_add_module(guiderGDS 
    src/pybind_GDS.cpp
    src/Decoder.cc
    src/Subscriber.cc
    src/TimeStats.cc
)
pybind11_add_module(guiderCentroid 
    src/pybind_guiderCentroid.cpp
    src/guiderCentroid.cpp
)
##pybind11_add_module(guiderProcessor src/pybind_guiderProcessor.cpp)

# EXAMPLE_VERSION_INFO is defined by setup.py and passed into the C++ code as a
# define (VERSION_INFO) here.
target_compile_definitions(guiderGDS
                           PRIVATE VERSION_INFO=${EXAMPLE_VERSION_INFO})
target_compile_definitions(guiderCentroid
                           PRIVATE VERSION_INFO=${EXAMPLE_VERSION_INFO})
##target_compile_definitions(guiderProcessor
##                           PRIVATE VERSION_INFO=${EXAMPLE_VERSION_INFO})
