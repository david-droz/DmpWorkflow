# pylint: disable=E1002
import logging
from datetime import datetime, timedelta
import sys
from mongoengine import CASCADE
from copy import deepcopy
from flask import url_for
from ast import literal_eval
from json import dumps
from numpy import array as np_array, median as np_median, mean as np_mean, histogram as np_hist
# from StringIO import StringIO
from DmpWorkflow.config.defaults import MAJOR_STATII, FINAL_STATII, TYPES, SITES
from DmpWorkflow.core import db
from DmpWorkflow.utils.tools import random_string_generator, exceptionHandler, datetime_to_js
from DmpWorkflow.utils.tools import parseJobXmlToDict, convertHHMMtoSec, sortTimeStampList

sys.excepthook = exceptionHandler
log = logging.getLogger("core")


class DataFile(db.Document):
    my_choices = ("New", "Copied", "Orphaned")
    created_at = db.DateTimeField(default=datetime.now, required=True)
    filename = db.StringField(max_length=1024, required=True)
    site = db.StringField(max_length=24, required=True)
    filetype = db.StringField(max_length=16, required=False, default="root")
    status = db.StringField(max_length=16, default="New")

    def setStatus(self, stat):
        if stat not in self.my_choices:
            raise Exception("status not supported in DB")
        self.status = stat

    def save(self):
        req = DataFile.objects.filter(filename=self.filename, site=self.site)
        if req:
            raise Exception("a file with the specified properties exists already, consider updating instead!")
        super(DataFile, self).save()

    def update(self):
        log.debug("calling update on DataFile")
        super(DataFile, self).save()

    meta = {
        'allow_inheritance': True,
        'indexes': ['-created_at', 'filename', 'site'],
        'ordering': ['-created_at']
    }


class HeartBeat(db.Document):
    ''' dummy class to test DB connection from remote workers '''
    created_at = db.DateTimeField(default=datetime.now, required=True)
    timestamp = db.DateTimeField(verbose_name="last sign of life", required=True)
    hostname = db.StringField(max_length=255, required=True)
    process  = db.StringField(max_length=64, required=False,default="default")
    deltat = db.FloatField(verbose_name="deltat",required=False,default=0.)
    version = db.StringField(max_length=32,verbose_name="version of package", required=False, default="None")
    meta = {
        'allow_inheritance': True,
        'indexes': ['-created_at', 'hostname','process'],
        'ordering': ['-created_at']
    }
    
    def checkStatus(self,deltaDays=1):
        now = datetime.now()
        margin = timedelta(days=deltaDays)
        p = now - margin <= self.timestamp <= now + margin
        return p

class Job(db.Document):
    created_at = db.DateTimeField(default=datetime.now, required=True)
    slug = db.StringField(verbose_name="slug", required=True, default=random_string_generator)
    #version = db.StringField(verbose_name="job version", required=False, default = "1.0")
    title = db.StringField(max_length=255, required=True)
    body = db.FileField()
    type = db.StringField(verbose_name="type", required=False, default="Other", choices=TYPES)
    release = db.StringField(max_length=255, required=False)
    dependencies = db.ListField(db.ReferenceField("Job"))
    execution_site = db.StringField(max_length=255, required=True, default="local", choices=SITES)
    jobInstances = db.ListField(db.ReferenceField("JobInstance"))
    archived = db.BooleanField(verbose_name="task closed", required=False, default=False)
    comment = db.StringField(max_length=1024, required=False, default="N/A")
    enable_monitoring = db.BooleanField(verbose_name="enable_monitoring", required=False, default=False)
    
    def aggregateResources(self,nbins=20):
        """ returns a json object which contains max, min, mean, median, 
            and the histogram itself for all memories/cpu 
            WARNING: this method is not particularly efficient 
            and shouldn't be used lightly!
        """
        allData = {"memory":{"data":[]},"cpu":{"data":[]}}
        query = JobInstance.objects.filter(job=self,status__not__exact="New").only("cpu").only("memory")
        if query.count():
            for inst in query:
                agg = inst.aggregateResources()
                for key in ['cpu','memory']:
                    if len(agg[key]):
                        allData[key]['data'].append(max(agg[key]))
            del query
            # finished aggregation, now we can do calculations                                                                                                                                              
            for key in allData:
                d = allData[key]["data"]
                if not len(d): 
                    continue
                allData[key]["max"]=max(d)
                allData[key]["min"]=min(d)
                arr = np_array(d,dtype=float)
                allData[key]["mean"]=float(np_mean(arr,axis=0))
                allData[key]["median"]=float(np_median(arr,axis=0))
                hist, bins = np_hist(arr,nbins)
                center = (bins[:-1] + bins[1:]) / 2
                w = (bins[1] - bins[0])
                histo = np_array([center, hist])
                allData[key]['histogram']={"histo":histo.tolist(),
                                           "histoT":histo.T.tolist(), "binWidth": float(w)}
                del allData[key]['data']
        return dumps(allData)

    
    def addDependency(self, job):
        if not isinstance(job, Job):
            raise Exception("Must be job to be added")
        self.dependencies.append(job)

    def archiveJob(self):
        self.archived = True

    def evalBody(self):
        evalKeys = ['InputFiles', 'OutputFiles', 'MetaData']
        meta = {}
        jobBody = self.getBody()
        for k in evalKeys:
            assert k in jobBody.keys(), "error, missing key %s in job body" % k
            meta[k] = jobBody[k]
        return meta
    
    def setDescription(self,desc):
        self.comment = desc

    def getOutputFiles(self):
        m = self.evalBody()
        out = [v['target'] for v in m['OutputFiles']]
        return out

    def getInputFiles(self):
        m = self.evalBody()
        out = [v['source'] for v in m['InputFiles']]
        return out

    def getMetaDataVariables(self):
        m = self.evalBody()
        out = {}
        for v in m['MetaData']: out.update({v['name']: v['value']})
        return out

    def getData(self):
        return self._data

    def getDependency(self, pretty=False):
        if not len(self.dependencies):
            return ()
        else:
            if pretty:
                return tuple([d.slug for d in self.dependencies])
            else:
                return tuple(self.dependencies)

    def getNevents(self):
        return self.getNeventsFast()

    def getNeventsFast(self):
        return JobInstance.objects.filter(job=self).aggregate_sum("Nevents")

    def getBody(self,setVars=False):
        # os.environ["DWF_JOBNAME"] = self.title
        bdy = deepcopy(self.body.get().read())
        self.body.get().seek(0)
        # bdy_file = StringIO(deepcopy(bdy))
        # self.body.delete()
        # self.body.put(bdy_file,content_type="application/xml")
        # self.update()
        return parseJobXmlToDict(bdy,setVars=setVars)

    def resetBody(self, body, content_type="application/xml"):
        self.body.replace(open(body, "rb"), content_type=content_type)
        self.save()

    def getInstance(self, _id):
        try:
            jI = JobInstance.objects.get(job=self, instanceId=_id)
            return jI
        except JobInstance.DoesNotExist:
            log.exception("could not find matching id")
        return None
        
    def addInstance(self, jInst, inst=None):
        if self.archived:
            raise Exception("cannot append new instances to job that is archived, must unlock first.")
        if len(self.jobInstances) >= 1000000:
            raise Exception("reached maximum of job instances, consider cloning this job instead.")
        if not isinstance(jInst, JobInstance):
            log.exception("must be job instance to be added")
            raise Exception("Must be job instance to be added")
        q = JobInstance.objects.filter(job=self).order_by("-instanceId")
        last_stream = 0
        if q.count():
            last_stream = int(q.first().instanceId)
        if inst is not None:
            # FIXME: offsets one, but then goes back to the length counter.
            last_stream = inst - 1
            if self.getInstance(last_stream + 1):
                log.exception("job with instance %i exists already", inst)
                raise Exception("job with instance %i exists already" % inst)
        jInst.instanceId = last_stream + 1
        if not len(jInst.status_history):
            sH = {"status": jInst.status, "update": jInst.last_update, "minor_status": jInst.minor_status}
            jInst.status_history.append(sH)
        jInst.job = self  # add self reference?
        # jInst.getResourcesFromMetadata()
        jInst.save()
        self.jobInstances.append(jInst)

    def addInstanceBulk(self,nreplica):
        """ using jInst as input instance, and creating a deepcopy of it, replicate it nreplica times and attach to job """
        if nreplica == 0: raise Exception("must be called with integer > 0.")
        isPilot = True if self.type == "Pilot" else False
        site = self.execution_site
        dummy_dict = {"InputFiles": [], "OutputFiles": [], "MetaData": []}
        query = JobInstance.objects.filter(job=self).order_by("-instanceId")
        inst_id = 1
        if query.count(): inst_id = query.first().instanceId 
        jInst = JobInstance(body=str(dummy_dict), site = site, isPilot=isPilot)
        sH = {"status": jInst.status, "update": jInst.last_update, "minor_status": jInst.minor_status}
        jInst.status_history.append(sH)
        jInst.job = self
        instances = []
        first = inst_id + 1
        last =  inst_id + nreplica + 1
        if last <= first: raise Exception("Must never happen")
        for i in xrange(first,last):
            jI = deepcopy(jInst)
            jI.instanceId = i
            instances.append(jI)
        query = JobInstance.objects.insert(instances)
        #print "added {added} instances to job {job}".format(job=self.title, added=len(instances))
        return len(instances)
        
    def aggregateStatii(self, asdict=False):
        # just an alias
        """ will return an aggregated summary of all instances in all statuses """
        return self.aggregateStatiiFast(asdict=asdict)

    def aggregateStatiiFast(self, asdict=False):
        """ will return an aggregated summary of all instances in all statuses """
        counting_dict = {unicode(key): 0 for key in MAJOR_STATII}
        counting_dict.update(JobInstance.objects.filter(job=self).item_frequencies("status"))
        if asdict:
            return counting_dict
        else:
            return [(key, value) for key, value in counting_dict.iteritems()]

    def countInstances(self):
        return JobInstance.objects.filter(job=self).count()

    def get_absolute_url(self):
        return url_for('job', kwargs={"slug": self.slug})

    def __unicode__(self):
        return self.title

    def delete(self):
        instances = JobInstance.objects.filter(job=self)
        self.body.delete()
        if len(instances):
            for ji in instances: ji.delete()
        super(Job, self).delete()

    #    def save(self):
    #        req = Job.objects.filter(title=self.title, type=self.type)
    #        if req:
    #            raise Exception("a task with the specified name & type exists already.")
    #        super(Job, self).save()
    
    def update(self):
        log.warning("deprecated method, use save")
        super(Job, self).save()

    meta = {
        'allow_inheritance': True,
        'indexes': ['-created_at', 'slug', 'title', 'id', 'execution_site'],
        'ordering': ['-created_at']
    }


class JobInstance(db.Document):
    #TODO: what is the most efficient way to store max_cpu, avg_cpu, curr_cpu (and mem)?
    instanceId = db.LongField(verbose_name="instanceId", required=False, default=None)
    created_at = db.DateTimeField(default=datetime.now, required=True)
    body = db.StringField(verbose_name="JobInstance", required=False, default="")
    last_update = db.DateTimeField(default=datetime.now, required=True)
    batchId = db.LongField(verbose_name="batchId", required=False, default=None)
    Nevents = db.LongField(verbose_name="Nevents", required=False, default=0)
    job = db.ReferenceField("Job", reverse_delete_rule=CASCADE)
    site = db.StringField(verbose_name="site", required=True, choices=SITES)
    hostname = db.StringField(verbose_name="hostname", required=False, default=None)
    status = db.StringField(verbose_name="status", required=False, default="New", choices=MAJOR_STATII)
    minor_status = db.StringField(verbose_name="minor_status", required=False, default="AwaitingBatchSubmission")
    status_history = db.ListField(db.DictField())
    memory = db.ListField(db.DictField())
    cpu = db.ListField(db.DictField())
    log = db.StringField(verbose_name="log", required=False, default="",help="last 20 lines of error messages")
    cpu_max = db.FloatField(verbose_name="maximal CPU time (seconds)", required=False, default=-1.)
    mem_max = db.FloatField(verbose_name="maximal memory (mb)", required=False, default=-1.)
    #doMonitoring = db.BooleanField(verbose_name="do_monitoring", required=True, default=job.enable_monitoring)

    # if the jobInstance is a normal "job" it has a pilot to refer to.
    # if the jobInstance is a pilot, "job" has a list of instances.
    isPilot      = db.BooleanField(verbose_name="is_pilot",required=False, default=False)
    pilotReference = db.ReferenceField("JobInstance") 

    def setAsPilot(self,val):
        self.isPilot = val
    
    def aggregateResources(self):
        """ returns dict of two arrays, first is memory, second is cpu """
        data = {"memory":[],"cpu":[]}
        my_map = {"memory":self.memory, "cpu":self.cpu}
        for key in data:
            data[key] = [item['value'] for item in my_map[key] if not isinstance(item['value'],list)]
        return data

    def getTimeStampCreatedAt(self,timeAsJS=True):
        if timeAsJS: return datetime_to_js(self.created_at)
        else: return self.created_at

    def getTimeStampLastUpdate(self,timeAsJS=True):
        if timeAsJS: return datetime_to_js(self.last_update)
        else: return self.last_update
            
    def getTimeSeries(self,key,timeAsJS=True):
        """ convenience function, returns two arrays,     
            one containing the x-axis, one the y-axis,
            userful for plotting with flot
            by default, timeStamps are converted to JavaTimeStampFormat.
        """
        def __getSeries__(key):
            """ just a neat little helper """
            if key == 'cpu': return self.cpu
            else: return self.memory
        
        if key not in ["cpu","memory"]: raise Exception("must be cpu or memory")
        data = []
        for item in __getSeries__(key):
            ds = []
            # ignore empty entries
            if not isinstance(item['value'],list):
                ts = item['time']
                if timeAsJS:
                    ts = datetime_to_js(ts)
                ds.append(ts)
                ds.append(item['value'])
            data.append(ds)
        return data
    
    def getStatusHistoryTimeStamps(self, timeAsJS=True):
        """ returns the list of status history items with js time stamps, and another array with strings """
        ts = []
        for item in self.status_history:
            tstamp = item['update']
            if timeAsJS: tstamp = datetime_to_js(tstamp)
            ts.append(tstamp)
        return ts
    
    def getStatusHistoryStats(self,key='minor_status'):
        if key not in ['minor_status','status']: raise NotImplementedError("must be status or minor_status")
        return dumps([item[key] for item in self.status_history])
    
    def resetJSON(self,set_var=None):
        """ convenience function: returns a JSON object that can be pushed to POST """
        override_dict = {"InputFiles": [], "OutputFiles": [], "MetaData": []}
        if set_var is not None:
            var_dict = dict({tuple(val.split("=")) for val in set_var.split(";")})
            override_dict['MetaData'] = [{"name": k, "value": v, "type": "str"} for k, v in var_dict.iteritems()]        
        my_dict = {"t_id": str(self.job.id), "inst_id": self.instanceId,
                   "major_status": "New", "minor_status": "AwaitingBatchSubmission", "hostname": None,
                   "batchId": None, "status_history": [], "body": str(override_dict),
                   "log": "", "cpu": [], "memory": [], "created_at": "Now"}
        return dumps(my_dict)

    def setBody(self, bdy):
        self.body = str(bdy)
        self.update()

    def __evalBody(self, includeParent=False):
        evalKeys = ['InputFiles', 'OutputFiles', 'MetaData']
        meta = {}
        if includeParent:
            meta.update(self.job.evalBody())
        inst_body = literal_eval(self.body)
        if not isinstance(inst_body, dict):
            raise Exception("Error in parsing body of JobInstance, not of type DICT")
        if len(inst_body):
            for k in evalKeys:
                assert k in inst_body.keys(), "error, missing key %s in instance body" % k
                if len(inst_body[k]):
                    meta[k] += inst_body[k]
        return meta

    def getOutputFiles(self, includeJob=True):
        m = self.__evalBody(includeParent=includeJob)
        out = []
        if len(m): out = [v['target'] for v in m['OutputFiles']]
        return out

    def getInputFiles(self, includeJob=True):
        m = self.__evalBody(includeParent=includeJob)
        out = []
        if len(m): out = [v['source'] for v in m['InputFiles']]
        return out

    def getMetaDataVariables(self, includeJob=True):
        m = self.__evalBody(includeParent=includeJob)
        out = {}
        if len(m):
            for v in m['MetaData']:
                out.update({v['name']: v['value']})
        return out

    def setMetaDataVariablesFromDict(self, _dict):
        if not isinstance(_dict, dict): raise Exception("Must be a dictionary!")
        bdy = literal_eval(self.body)
        for k, v in _dict.iteritems(): bdy['MetaData'].append({'value': v, 'name': k, 'type': 'str'})
        self.set("body", str(bdy))

    def getResourcesFromMetadata(self):
        md = []
        res = {"BATCH_OVERRIDE_CPUTIME": self.cpu_max, "BATCH_OVERRIDE_MEMORY": self.mem_max}
        var_map = {"BATCH_OVERRIDE_CPUTIME": "cpu_max", "BATCH_OVERRIDE_MEMORY": "mem_max"}
        metadata = self.job.getBody()
        if isinstance(metadata, dict):
            if 'MetaData' in metadata:
                md = metadata['MetaData']
        if self.body != "":
            instance_dict = literal_eval(self.body)
            if 'MetaData' in instance_dict:
                md += instance_dict['MetaData']
                # next, set the values
        for v in md:
            if v['name'] in var_map:
                val = v['value']
                if ":" in val: val = convertHHMMtoSec(val)
                res[v['name']] = float(val)
        for k, v in var_map.iteritems():
            self.set(v, res[k])
        return

    def getWallTime(self, unit='s'):
        if self.status == "New": return 0.
        if self.status not in FINAL_STATII:
            log.warning("job not find in final status, CPU time may not be accurate")
        dt1 = self.status_history[0]['update']
        dt2 = self.status_history[1]['update']
        total_sec = (dt2 - dt1).total_seconds()
        if unit == "min":
            return float(total_sec) / 60.
        elif unit == "hrs":
            return float(total_sec) / 3600.
        else:
            if unit != "s":
                log.warning("unsupported unit, returning seconds")
            return total_sec

    def getCpuTime(self, unit='s'):
        if self.status == "New": return 0.
        if self.status not in FINAL_STATII:
            log.debug("job not find in final status, CPU time may not be accurate")
        total_sec = self.cpu[-1]['value']
        if unit == "min":
            return float(total_sec) / 60.
        elif unit == "hrs":
            return float(total_sec) / 3600.
        else:
            if unit != "s":
                log.warning("unsupported unit, returning seconds")
            return total_sec

    def getEfficiency(self):
        cpt = self.getCpuTime()
        wct = self.getWallTime()
        eff = cpt / wct
        return eff

    def getMemory(self, method='average'):
        """ get memory of job in Mb """
        if self.status == "New": return 0.
        if self.status not in FINAL_STATII:
            log.debug("job not find in final status, result may not be accurate")
        assert method in ['average', 'min', 'max'], "method not supported"
        all_memory = [float(v["value"]) for v in self.memory if not isinstance(v["value"],list)]
        if method == 'min':
            return min(all_memory)
        elif method == 'max':
            return max(all_memory)
        else:
            return sum(all_memory) / float(len(all_memory))

    def checkDependencies(self, check_status=u"Done"):
        dependent_tasks = self.job.getDependency()
        isReady = True
        for task in dependent_tasks:
            inst = task.getInstance(self.instanceId)
            if inst.status != check_status: isReady = False
        return isReady

    #    def parseBodyXml(self,key="MetaData"):
    #        p = parseJobXmlToDict(self.job.body.read())
    #        return p[key]

    def getLog(self):
        lines = self.log.split("\n")
        return lines

    def get(self, key):
        if key in ['cpu_max', 'mem_max']:
            if key not in self._data.keys():
                return 0.
            else:
                return float(self._data.get(key))
        elif key == 'cpu':
            # -1 doesn't appear to be a valid key
            if not len(self.cpu): return 0.
            index = len(self.cpu) - 1
            if index < 0: index = 0
            return self.cpu[index]['value']
        elif key == 'memory':
            if not len(self.memory): return 0.
            index = len(self.memory) - 1
            if index < 0: index = 0
            return self.memory[index]['value']
        else:
            return 0.

    def set(self, key, value):
        if key == 'minor_status':
            raise NotImplementedError("use JobInstance.setStatus(major_status,minorStatus) instead.")
        elif key == "created_at" and value == "Now":
            value = datetime.now()
        elif key == 'cpu':
            self.cpu.append({"time": datetime.now(), "value": value})
        elif key == 'memory':
            self.memory.append({"time": datetime.now(), "value": value})
        elif key in ['cpu_max', 'mem_max']:
            #self._data.__setitem__(key, float(value))                                                                                                                                                               
            ret = 0
            if key == 'cpu_max':
                ret = JobInstance.objects.filter(job=self.job,instanceId=self.instanceId).update(cpu_max=value)
            else:
                ret = JobInstance.objects.filter(job=self.job,instanceId=self.instanceId).update(mem_max=value)
            if ret!=1:
                log.critical("ERROR: JobInstance::set(%s,%s) returned %i",key,value,ret)
                raise Exception("ERROR: JobInstance::set(%s,%s), returned %i"%(key,value,ret))
            self.update()
        else:
            self.__setattr__(key, value)
        log.debug("setting %s : %s", key, value)
        self.__setattr__("last_update", datetime.now())
        self.update()

    def __logHistory__(self):
        """ does the accounting in status_history field """
        stat = self.status
        minStat  = self.minor_status
        upd  = self.last_update
        item = {"status":stat, "minor_status": minStat, "update":upd}

        history_so_far = self.status_history
        appd = False
        if len(history_so_far): appd = True
        elif self.getHistoryMinorStatusLast() != minStat: appd = True
        if appd: history_so_far.append(item)
        else:
            return
        # finally, perform atomic update
        try:
            res = JobInstance.objects.filter(instanceId=self.instanceId, job=self.job).update(status_history=history_so_far)
            if res!= 1: raise Exception("could not perform atomic update on status history.")
        except Exception as err:
            log.exception("JobInstance.__logHistory__() trew error, %s",err)
            raise Exception(err)
        return
    
    def getHistoryMinorStatusLast(self):
        """ returns the last minor status """
        item = None
        for item in self.status_history:
            if 'minor_status' not in item:
                log.error("minor_status field missing in status_history, item %s",str(item))
        if item is not None:
            return item['minor_status']

    def setStatus(self, stat, minorStatus):
        log.debug("calling JobInstance.setStatus")
        if stat not in MAJOR_STATII:
            raise Exception("status not found in supported list of statii: %s", stat)
        curr_status = self.status
        if curr_status in FINAL_STATII: 
            self.__sortTimeStampedLists()
            self.update()
        curr_minor  = self.minor_status
        if minorStatus is None:
            log.warning("no minorStatus provided, assuming last minor status!")
            minorStatus = curr_minor
        if curr_status == stat and curr_minor == minorStatus: 
            # nothing has changed
            return
        if curr_status in FINAL_STATII:
            if not stat == 'New':
                exc = "job found in final state, can only set to New"
                log.error(exc)
                raise Exception(exc)
            return
        else:
            # now store the old status in the history
            self.__logHistory__()
        q = {"job":self.job, "instanceId":self.instanceId}
        upd_dict = {"status":stat, "minor_status":minorStatus, "last_update":datetime.now()}
        ret = JobInstance.objects.filter(**q).update(**upd_dict)
        if ret != 1:
            raise Exception("error setting status")
        return

    def __sortTimeStampedLists(self):
        # final step - sort time-stamped lists to be chronological
        if len(self.status_history) > 1:
            self.status_history = sortTimeStampList(self.status_history, timestamp="update")
        if len(self.cpu) > 1:
            self.cpu = sortTimeStampList(self.cpu)
        if len(self.memory) > 1:
            self.memory = sortTimeStampList(self.memory)
        return

    def sixDigit(self, size=6):
        return str(self.instanceId).zfill(size)

    def update(self):
        log.debug("calling update on JobInstance")
        super(JobInstance, self).save()

    def save(self):
        req = JobInstance.objects.filter(job=self.job, instanceId=self.instanceId)
        if req:
            raise Exception("instance exists already.")
        super(JobInstance, self).save()

    meta = {
        'allow_inheritance': True,
        'indexes': ['-created_at', 'instanceId', 'site'],
        'ordering': ['-created_at']
    }
