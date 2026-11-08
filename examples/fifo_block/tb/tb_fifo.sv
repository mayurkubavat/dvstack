// Self-checking FIFO test with a scoreboard. Seed comes from +seed=N.
module tb_fifo;
  localparam int WIDTH = 8, DEPTH = 4;
  logic clk = 0, rst_n = 0, push = 0, pop = 0;
  logic [WIDTH-1:0] wdata = '0, rdata;
  logic full, empty;
  int unsigned seed = 1;
  int errors = 0, pushes = 0, pops = 0, cycles = 2000;
  logic [WIDTH-1:0] model [$];

  fifo #(.WIDTH(WIDTH), .DEPTH(DEPTH)) dut (.*);

  always #5 clk = ~clk;

  initial begin
    void'($value$plusargs("seed=%d", seed));
    void'($value$plusargs("cycles=%d", cycles));
    void'($urandom(seed));
    repeat (3) @(posedge clk);
    rst_n = 1;
    repeat (cycles) begin
      @(negedge clk);
      push  = ($urandom % 100) < 55;
      pop   = ($urandom % 100) < 45;
      wdata = $urandom;
      @(posedge clk);
      #1;
    end
    if (pushes == 0 || pops == 0) begin
      $display("ERROR: stimulus never pushed or popped");
      errors++;
    end
    if (errors == 0) $display("TEST PASSED seed=%0d pushes=%0d pops=%0d", seed, pushes, pops);
    else             $display("TEST FAILED seed=%0d errors=%0d", seed, errors);
    $finish;
  end

  // Scoreboard: sample on the clock edge, before the DUT updates.
  always @(posedge clk) if (rst_n) begin
    if (pop && !empty) begin
      if (model.size() == 0) begin
        $display("ERROR @%0t: pop from empty model", $time); errors++;
      end else begin
        if (rdata !== model[0]) begin
          $display("ERROR @%0t: rdata=%0h expected=%0h", $time, rdata, model[0]); errors++;
        end
        void'(model.pop_front());
        pops++;
      end
    end
    if (push && !full) begin
      model.push_back(wdata);
      pushes++;
    end
  end

  // Check flags against the model every cycle after the update settles.
  always @(negedge clk) if (rst_n) begin
    if (full !== (model.size() == DEPTH)) begin
      $display("ERROR @%0t: full=%0b model_size=%0d", $time, full, model.size()); errors++;
    end
    if (empty !== (model.size() == 0)) begin
      $display("ERROR @%0t: empty=%0b model_size=%0d", $time, empty, model.size()); errors++;
    end
  end

  initial if ($test$plusargs("trace")) begin
    $dumpfile("sim.vcd");
    $dumpvars(0, tb_fifo);
  end
endmodule
