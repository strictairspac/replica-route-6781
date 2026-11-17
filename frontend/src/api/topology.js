import {api} from './client.js';
export const topologyApi={
  get:()=>api('/topology'),
  versions:()=>api('/topology/versions'),
  version:id=>api('/topology/versions/'+id),
  changes:()=>api('/topology/changes'),
  change:id=>api('/topology/changes/'+id),
  create:data=>api('/topology/changes','POST',data),
  update:(id,data)=>api('/topology/changes/'+id,'PUT',data),
  validate:id=>api('/topology/changes/'+id+'/validate','POST'),
  resolve:(id,data)=>api('/topology/changes/'+id+'/resolve','POST',data),
  publish:(id,data)=>api('/topology/changes/'+id+'/publish','POST',data),
  cancel:id=>api('/topology/changes/'+id+'/cancel','POST'),
  recover:()=>api('/topology/recover','POST')
};
