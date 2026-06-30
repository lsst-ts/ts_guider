# GoogleTest unit tests for the C++ side of guiderGDS.
#
# GoogleTest (and gmock) ship in the LSST conda stack used by the
# lsstts/develop-env image, so find_package(GTest) needs no download.
# CI consumes JUnit XML emitted by ctest --output-junit.

find_package(GTest REQUIRED)
include(GoogleTest)

# One executable per test_*.cpp file. gtest_discover_tests registers
# each TEST() with CTest automatically.
file(GLOB TEST_SOURCES CONFIGURE_DEPENDS "test_*.cpp")

foreach(test_source ${TEST_SOURCES})
    get_filename_component(test_name "${test_source}" NAME_WE)
    add_executable(${test_name} "${test_source}")
    target_include_directories(${test_name}
        PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/../src")
    target_link_libraries(${test_name} PRIVATE GTest::gtest_main)
    gtest_discover_tests(${test_name})
endforeach()
