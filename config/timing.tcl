read_liberty /home/samhita_reddy_k/timing_ml/lib/NangateOpenCellLibrary_typical.lib
read_verilog results/adder_mapped.v
link_design top

read_sdc scripts/adder.sdc

report_units
report_checks -path_delay max -format full_clock_expanded -digits 4
report_worst_slack -max -digits 4
report_wns -max -digits 4
report_tns -max -digits 4

exit
