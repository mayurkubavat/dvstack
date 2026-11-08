// Synchronous FIFO used as the reference block for the dv plugin.
// FIFO_BUG_* defines inject known faults so dv:verify can prove its
// tests and assertions are able to fail (anti-vacuity).
module fifo #(
  parameter int WIDTH = 8,
  parameter int DEPTH = 4
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic             push,
  input  logic [WIDTH-1:0] wdata,
  input  logic             pop,
  output logic [WIDTH-1:0] rdata,
  output logic             full,
  output logic             empty
);
  localparam int AW = $clog2(DEPTH);

  logic [WIDTH-1:0] mem [DEPTH];
  logic [AW-1:0]    wptr, rptr;
  logic [AW:0]      count;

  wire do_push = push && !full;
  wire do_pop  = pop && !empty;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      wptr  <= '0;
      rptr  <= '0;
      count <= '0;
    end else begin
      if (do_push) begin
        mem[wptr] <= wdata;
        wptr      <= wptr + 1'b1;
      end
      if (do_pop) begin
`ifdef FIFO_BUG_DOUBLE_POP
        rptr <= rptr + 2'd2;
`else
        rptr <= rptr + 1'b1;
`endif
      end
      count <= count + (do_push ? 1 : 0) - (do_pop ? 1 : 0);
    end
  end

  assign rdata = mem[rptr];
`ifdef FIFO_BUG_FULL_EARLY
  assign full  = (count >= (AW+1)'(DEPTH - 1));
`else
  assign full  = (count == (AW+1)'(DEPTH));
`endif
  assign empty = (count == '0);

  // Properties. Each assert has a cover so a pass can't be vacuous.
  a_no_overflow: assert property (@(posedge clk) disable iff (!rst_n) count <= (AW+1)'(DEPTH));
  a_full_empty_excl: assert property (@(posedge clk) disable iff (!rst_n) !(full && empty));
  a_full_iff_depth: assert property (@(posedge clk) disable iff (!rst_n) full == (count == (AW+1)'(DEPTH)));
  a_ptr_count: assert property (@(posedge clk) disable iff (!rst_n) (wptr - rptr) == count[AW-1:0]);
  a_full_holds: assert property (@(posedge clk) disable iff (!rst_n) full && push && !pop |=> full);
  c_full:  cover property (@(posedge clk) disable iff (!rst_n) full);
  c_push_pop: cover property (@(posedge clk) disable iff (!rst_n) do_push && do_pop);
  c_push_when_full: cover property (@(posedge clk) disable iff (!rst_n) full && push);
endmodule
