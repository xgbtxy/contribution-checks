add_llvm_executable(mlir-round-expand-opt round-expand-opt.cpp)

get_filename_component(MLIR_SOURCE_ROOT "${LLVM_MAIN_SRC_DIR}/../mlir" ABSOLUTE)
set(MLIR_GENERATED_INCLUDE_DIR "${LLVM_BINARY_DIR}/tools/mlir/include")

target_include_directories(mlir-round-expand-opt PRIVATE
  ${LLVM_INCLUDE_DIRS}
  ${MLIR_INCLUDE_DIRS}
  "${MLIR_SOURCE_ROOT}/include"
  "${MLIR_GENERATED_INCLUDE_DIR}"
)

target_link_libraries(mlir-round-expand-opt PRIVATE
  MLIROptLib
  MLIRMathTransforms
  MLIRArithDialect
  MLIRComplexDialect
  MLIRFuncDialect
  MLIRMathDialect
  MLIRSCFDialect
  MLIRTensorDialect
  MLIRVectorDialect
)

add_dependencies(mlir-round-expand-opt MLIRMathTransformsIncGen)
