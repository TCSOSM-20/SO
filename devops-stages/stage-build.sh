#!/bin/sh
rm -rf .build
make NOT_DEVELOPER_BUILD=TRUE -j16 package

