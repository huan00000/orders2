cmake_minimum_required(VERSION 3.20)
project(orders2 VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
find_package(SQLite3 3.35 REQUIRED)
if(NOT TARGET SQLite3::SQLite3)
  add_library(SQLite3::SQLite3 ALIAS SQLite::SQLite3)
endif()
find_package(Threads REQUIRED)
add_library(orders2_core
  src/domain/order_state.cpp
  src/storage/database.cpp
  src/storage/store.cpp
  src/config/config.cpp)
target_include_directories(orders2_core PUBLIC include)
target_link_libraries(orders2_core PUBLIC SQLite3::SQLite3 Threads::Threads)
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(orders2_core PRIVATE -Wall -Wextra -Wpedantic -Wconversion)
endif()
include(CTest)
if(BUILD_TESTING)
  add_executable(orders2_foundation_tests tests/foundation_test.cpp)
  target_link_libraries(orders2_foundation_tests PRIVATE orders2_core)
  add_test(NAME foundation COMMAND orders2_foundation_tests)
  set_tests_properties(foundation PROPERTIES TIMEOUT 30)
endif()
