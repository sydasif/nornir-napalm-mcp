# Class base refactor plan

- main class for nornir
  - class name: NornirBase
  - shared instance
  - related functions

- child class for napalm
  - class name: NapalmTool
  - inherits from NornirMcp
  - functions for each tool

- child class for netmiko
  - class name: NetmikoTool
  - inherits from NornirMcp
  - related tool functions for each tool

This plan is complete restructuring of the nornir-mcp code base. The main goal is to create a more organized and maintainable structure by separating the core functionality into a base class and creating specific tool classes for different libraries like Napalm and Netmiko.
