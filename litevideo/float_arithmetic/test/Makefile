HDLDIR = ../../../
PYTHON = python3

CMD = PYTHONPATH=$(HDLDIR) $(PYTHON)

floatmult_tb:
	$(CMD) floatmult_tb.py

floatadd_tb:
	$(CMD) floatadd_tb.py

clean:
	rm -rf *_*.png *.vvp *.v *.vcd

.PHONY: clean