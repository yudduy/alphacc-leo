obj-m += tcp_evolved.o

KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	make -C $(KDIR) M=$(PWD) modules

clean:
	make -C $(KDIR) M=$(PWD) clean

install:
	sudo insmod tcp_evolved.ko

uninstall:
	sudo rmmod tcp_evolved

.PHONY: all clean install uninstall
