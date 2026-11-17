import {topologyApi} from '../api/topology.js';
export const topologyStore={
  head:null,versions:[],changes:[],
  async refresh(){const [topology,changes]=await Promise.all([topologyApi.get(),topologyApi.changes()]);this.head=topology.head;this.versions=topology.versions;this.changes=changes;return this;},
  async refreshVersions(){const topology=await topologyApi.get();this.head=topology.head;this.versions=topology.versions;return this;}
};
