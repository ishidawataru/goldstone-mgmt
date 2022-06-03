.PHONY: builder bash init yang base-image images docker cli system

ARCH ?= amd64

DOCKER_CMD ?= bash
DOCKER_BUILD_OPTION ?= --platform linux/$(ARCH)
DOCKER_IMAGE ?= $(BUILDER)

GS_MGMT_IMAGE_PREFIX ?= ghcr.io/oopt-goldstone/mgmt/
GS_MGMT_IMAGE_TAG ?= latest-$(ARCH)

GS_MGMT_BUILDER_IMAGE ?= builder
GS_MGMT_BASE_IMAGE    ?= base
GS_MGMT_SONIC_IMAGE   ?= south-sonic
GS_MGMT_TAI_IMAGE     ?= south-tai
GS_MGMT_ONLP_IMAGE    ?= south-onlp
GS_MGMT_SYSTEM_IMAGE  ?= south-system
GS_MGMT_GEARBOX_IMAGE ?= south-gearbox
GS_MGMT_DPLL_IMAGE    ?= south-dpll
GS_MGMT_CLI_IMAGE     ?= north-cli
GS_MGMT_SNMP_IMAGE    ?= north-snmp
GS_MGMT_SNMPD_IMAGE   ?= snmpd
GS_MGMT_NP2_IMAGE     ?= north-netconf
GS_MGMT_NOTIF_IMAGE   ?= north-notif
GS_MGMT_OC_IMAGE      ?= xlate-openconfig
GS_MGMT_TEST_IMAGE    ?= tester
GS_MGMT_HOST_IMAGE    ?= host

define image_name
$(GS_MGMT_IMAGE_PREFIX)$1:$(GS_MGMT_IMAGE_TAG)
endef

GS_SAVE_AFTER_BUILD ?= 0

define save
if [ $(GS_SAVE_AFTER_BUILD) -eq 1 ]; then mkdir -p builds && docker save $(call image_name,$(1)) > builds/$(1)-$(ARCH).tar && gzip -f builds/$(1)-$(ARCH).tar; fi
endef

BUILDER ?= $(call image_name,$(GS_MGMT_BUILDER_IMAGE))
BASE ?= $(call image_name,$(GS_MGMT_BUILDER_IMAGE))
TAI_META_CUSTOM_FILES ?= $(abspath $(wildcard scripts/tai/*))

TRANSPONDER_YANG ?= ./yang/goldstone-transponder.yang

all: builder base-image images tester host-packages

builder:
	DOCKER_BUILDKIT=1 docker build $(DOCKER_BUILD_OPTION) -f docker/builder.Dockerfile -t $(BUILDER) .
	$(call save,$(GS_MGMT_BUILDER_IMAGE))

base-image:
	DOCKER_BUILDKIT=1 docker build $(DOCKER_BUILD_OPTION) -f docker/base.Dockerfile \
							      --build-arg GS_MGMT_BUILDER_IMAGE=$(BUILDER) \
							      -t $(call image_name,$(GS_MGMT_BASE_IMAGE)) .
	$(call save,$(GS_MGMT_BASE_IMAGE))

host-packages:
	DOCKER_BUILDKIT=1 docker build $(DOCKER_BUILD_OPTION) -f docker/host.Dockerfile \
							      --build-arg GS_MGMT_BUILDER_IMAGE=$(BUILDER) \
							      -t $(call image_name,$(GS_MGMT_HOST_IMAGE)) .
	$(call save,$(GS_MGMT_HOST_IMAGE))

images: south-images north-images xlate-images

south-images: south-sonic south-tai south-onlp south-system south-gearbox south-dpll

north-images: north-cli north-snmp north-netconf north-notif

xlate-images: xlate-oc

image:
	DOCKER_BUILDKIT=1 docker build $(DOCKER_BUILD_OPTION) -f $(DOCKER_FILE) \
							      --build-arg GS_MGMT_BUILDER_IMAGE=$(BUILDER) \
							      --build-arg GS_MGMT_BASE=$(call image_name,$(GS_MGMT_BASE_IMAGE)) \
							      -t $(call image_name,$(IMAGE_NAME)) .
	$(call save,$(IMAGE_NAME))

south-sonic:
	IMAGE_NAME=$(GS_MGMT_SONIC_IMAGE) DOCKER_FILE=docker/south-sonic.Dockerfile $(MAKE) image

south-tai:
	IMAGE_NAME=$(GS_MGMT_TAI_IMAGE) DOCKER_FILE=docker/south-tai.Dockerfile $(MAKE) image

south-gearbox:
	IMAGE_NAME=$(GS_MGMT_GEARBOX_IMAGE) DOCKER_FILE=docker/south-gearbox.Dockerfile $(MAKE) image

south-dpll:
	IMAGE_NAME=$(GS_MGMT_DPLL_IMAGE) DOCKER_FILE=docker/south-dpll.Dockerfile $(MAKE) image

south-onlp:
	IMAGE_NAME=$(GS_MGMT_ONLP_IMAGE) DOCKER_FILE=docker/south-onlp.Dockerfile $(MAKE) image

south-system:
	IMAGE_NAME=$(GS_MGMT_SYSTEM_IMAGE) DOCKER_FILE=docker/south-system.Dockerfile $(MAKE) image

north-cli:
	IMAGE_NAME=$(GS_MGMT_CLI_IMAGE) DOCKER_FILE=docker/north-cli.Dockerfile $(MAKE) image

north-notif:
	IMAGE_NAME=$(GS_MGMT_NOTIF_IMAGE) DOCKER_FILE=docker/north-notif.Dockerfile $(MAKE) image

north-snmp: snmpd
	IMAGE_NAME=$(GS_MGMT_SNMP_IMAGE) DOCKER_FILE=docker/north-snmp.Dockerfile $(MAKE) image

north-netconf:
	IMAGE_NAME=$(GS_MGMT_NP2_IMAGE) DOCKER_FILE=docker/netopeer2.Dockerfile $(MAKE) image

xlate-oc:
	IMAGE_NAME=$(GS_MGMT_OC_IMAGE) DOCKER_FILE=docker/xlate-openconfig.Dockerfile $(MAKE) image

snmpd:
	IMAGE_NAME=$(GS_MGMT_SNMPD_IMAGE) DOCKER_FILE=docker/snmpd.Dockerfile $(MAKE) image

tester:
	IMAGE_NAME=$(GS_MGMT_TEST_IMAGE) DOCKER_FILE=docker/builder.Dockerfile DOCKER_BUILD_OPTION="$(DOCKER_BUILD_OPTION) --target tester" $(MAKE) image

yang:
	./tools/tai_yang_gen.py ./sm/oopt-tai/inc/tai.h $(TAI_META_CUSTOM_FILES) | pyang -f yang > $(TRANSPONDER_YANG)

bash:
	DOCKER_RUN_OPTION='-it --cap-add IPC_OWNER --cap-add IPC_LOCK' $(MAKE) cmd

tester-bash:
	DOCKER_RUN_OPTION='-it' DOCKER_IMAGE=$(call image_name,$(GS_MGMT_TEST_IMAGE)) $(MAKE) cmd

cmd:
	docker run $(DOCKER_RUN_OPTION) -v `pwd`:/data -w /data -v /etc/onl/platform:/etc/onl/platform $(DOCKER_IMAGE) $(DOCKER_CMD)

lint:
	which black && exit `black -q --diff --exclude src/north/snmp/src src | wc -l`
	TRANSPONDER_YANG=/tmp/test.yang $(MAKE) yang && diff /tmp/test.yang $(TRANSPONDER_YANG)
	scripts/gs-yang.py --lint south-sonic south-onlp south-tai south-system xlate-oc --search-dirs yang sm/openconfig
	scripts/gs-yang.py --lint south-gearbox south-onlp south-tai south-system xlate-oc --search-dirs yang sm/openconfig
	grep -rnI 'print(' src || exit 0 && exit 1

unittest: unittest-lib unittest-cli unittest-gearbox unittest-dpll unittest-openconfig unittest-tai unittest-sonic
	cd src/south/sonic && make proto
	scripts/gs-yang.py --install south-sonic south-tai --search-dirs yang
	PYTHONPATH=src/lib:src/south/sonic:src/south/tai python -m unittest -v -f && rm -rf /dev/shm/sr* /var/lib/sysrepo
	cd src/south/sonic      && make clean

unittest-lib:
	sysrepoctl --search-dirs yang --install yang/goldstone-interfaces.yang
	sysrepoctl --search-dirs yang --install yang/goldstone-transponder.yang
	cd src/lib && python -m unittest -v -f && rm -rf /dev/shm/sr* /var/lib/sysrepo

unittest-cli:
	sysrepoctl --search-dirs yang --install yang/goldstone-interfaces.yang
	sysrepoctl --search-dirs yang --install yang/goldstone-synce.yang
	sysrepoctl --search-dirs yang --install yang/goldstone-static-macsec.yang
	sysrepoctl --search-dirs yang --install yang/goldstone-vlan.yang
	sysrepoctl --search-dirs yang --install yang/goldstone-uplink-failure-detection.yang
	sysrepoctl --search-dirs yang --install yang/goldstone-portchannel.yang
	sysrepoctl --search-dirs yang --install yang/goldstone-transponder.yang
	cd src/north/cli        && PYTHONPATH=../../lib python -m unittest -v -f && rm -rf /dev/shm/sr* /var/lib/sysrepo

unittest-gearbox:
	scripts/gs-yang.py --install south-gearbox --search-dirs yang
	cd src/south/gearbox    && PYTHONPATH=../../lib python -m unittest -v -f && rm -rf /dev/shm/sr* /var/lib/sysrepo

unittest-dpll:
	scripts/gs-yang.py --install south-dpll --search-dirs yang
	cd src/south/dpll && PYTHONPATH=../../lib python -m unittest -v -f && rm -rf /dev/shm/sr* /var/lib/sysrepo

unittest-openconfig:
	scripts/gs-yang.py --install xlate-oc south-sonic --search-dirs yang sm/openconfig
	cd src/xlate/openconfig && PYTHONPATH=../../lib python -m unittest -v -f && rm -rf /dev/shm/sr* /var/lib/sysrepo

unittest-tai:
	scripts/gs-yang.py --install south-tai --search-dirs yang
	cd src/south/tai        && PYTHONPATH=../../lib python -m unittest -v -f && rm -rf /dev/shm/sr* /var/lib/sysrepo

unittest-sonic:
	cd src/south/sonic && make proto
	scripts/gs-yang.py --install south-sonic --search-dirs yang
	cd src/south/sonic      && PYTHONPATH=../../lib python -m unittest -v -f && rm -rf /dev/shm/sr* /var/lib/sysrepo
	cd src/south/sonic      && make clean

release:
	./tools/release.py
