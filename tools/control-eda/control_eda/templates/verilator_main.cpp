// Verilator main used by control-eda: runs to $finish, then writes coverage.
#include "verilated.h"
#include "verilated_cov.h"
#include "Vtop.h"  // control-eda always builds with --prefix Vtop

int main(int argc, char** argv) {
    const std::unique_ptr<VerilatedContext> ctx{new VerilatedContext};
    ctx->commandArgs(argc, argv);
#if VM_TRACE
    ctx->traceEverOn(true);
#endif
    const std::unique_ptr<Vtop> top{new Vtop{ctx.get()}};
    while (!ctx->gotFinish()) {
        top->eval();
        if (!top->eventsPending()) break;
        ctx->time(top->nextTimeSlot());
    }
    top->final();
#if VM_COVERAGE
    const char* path = getenv("CONTROL_EDA_COV_FILE");
    ctx->coveragep()->write(path ? path : "coverage.dat");
#endif
    return 0;
}
