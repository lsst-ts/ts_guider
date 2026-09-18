# This file is part of ts_guider.
#
# Developed for Vera C. Rubin Observatory Telescope and Site Systems.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License

__all__ = ["CONFIG_SCHEMA"]

import yaml

CONFIG_SCHEMA = yaml.safe_load("""
$schema: http://json-schema.org/draft-07/schema#
$id: https://github.com/lsst-ts/ts_guider/blob/main/python/lsst/ts/guider/config_schema.py
# title must end with one or more spaces followed by the schema version, which must begin with "v"
title: Guider v1
description: Schema for Guider configuration files
type: object
properties:
  partition:
    description: DAQ/GDS partition to subscribe to for guider stamps.
    type: string
  seed_frames:
    description: >-
      Number of stamps buffered per sensor before locking a guide star.
      Optional; defaults to the pipeline value when omitted.
    type: integer
    minimum: 1
  min_snr:
    description: >-
      Minimum HSM signal-to-noise ratio for a stamp measurement to
      pass the quality cuts. Optional; defaults to the pipeline value
      when omitted.
    type: number
required:
  - partition
additionalProperties: false
""")
