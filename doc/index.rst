.. py:currentmodule:: lsst.ts.guider

.. _lsst.ts.guider:

###################
lsst.ts.guider
###################

.. image:: https://img.shields.io/badge/Project Metadata-gray.svg
    :target: https://ts-xml.lsst.io/index.html#index-master-csc-table-guider
.. image:: https://img.shields.io/badge/SAL\ Interface-gray.svg
    :target: https://ts-xml.lsst.io/sal_interfaces/guider.html
.. image:: https://img.shields.io/badge/GitHub-gray.svg
    :target: https://github.com/lsst-ts/ts_guider
.. image:: https://img.shields.io/badge/Jira-gray.svg
    :target: https://jira.lsstcorp.org/issues/?jql=labels+%3D+ts_guider

Overview
========

The guider CSC.

User Guide
==========

Start the guider CSC
-------------------------

.. prompt:: bash

    run_guider

.. _lsst.ts.guider.configuration:

Configuration
-------------

Configuration is specified in `ts_config_mttcs <https://github.com/lsst-ts/ts_config_mttcs>`_ following `this schema <https://github.com/lsst-ts/ts_guider/blob/develop/python/lsst/ts/guider/config_schema.py>`_.

Simulator
---------


To run using CSC's internal simulator:

.. prompt:: bash

    run_guider --simulate={mode}

.. _lsst.ts.guider.enable_with_eui:

Developer Guide
===============

.. toctree::
    developer_guide
    :maxdepth: 1

Version History
===============

.. toctree::
    version_history
    :maxdepth: 1
