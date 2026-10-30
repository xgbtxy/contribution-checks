//===- round-expand-opt.cpp - Narrow MLIR math expansion driver --------===//
//
// Validation harness linked against LLVM/MLIR; not an upstream LLVM tool.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
//
//===----------------------------------------------------------------------===//

#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Complex/IR/Complex.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Dialect/Math/IR/Math.h"
#include "mlir/Dialect/Math/Transforms/Passes.h"
#include "mlir/Dialect/SCF/IR/SCF.h"
#include "mlir/Dialect/Tensor/IR/Tensor.h"
#include "mlir/Dialect/Vector/IR/VectorOps.h"
#include "mlir/IR/DialectRegistry.h"
#include "mlir/Tools/mlir-opt/MlirOptMain.h"

int main(int argc, char **argv) {
  mlir::DialectRegistry registry;
  registry.insert<mlir::arith::ArithDialect, mlir::complex::ComplexDialect,
                  mlir::func::FuncDialect, mlir::math::MathDialect,
                  mlir::scf::SCFDialect, mlir::tensor::TensorDialect,
                  mlir::vector::VectorDialect>();

  mlir::math::registerMathExpandOpsPass();
  return mlir::asMainReturnCode(mlir::MlirOptMain(
      argc, argv, "Narrow LLVM/MLIR math expansion validator\n", registry));
}
