"""
Created on Mar 22, 2016

@author: zimmer
"""
import logging
from subprocess import PIPE, Popen
from select import poll as spoll, POLLIN, POLLHUP
from tempfile import NamedTemporaryFile
from os import chmod, stat, environ, remove
from os.path import expandvars

logger = logging.getLogger("core")
def run(cmd_args, useLogging=True, suppressErrors=False, interleaved=True, suppressLevel=False, cache=False, chunksize=36):
    # inspired from http://tinyurl.com/hslhjfe (StackOverflow)
    if not isinstance(cmd_args, list):
        raise RuntimeError('must be list to be called')
    logger.info("attempting to run: %s",str(cmd_args))
    args = [[],[]] # first is output, second is errors
    tsk = Popen(cmd_args,stdout=PIPE,stderr=PIPE)
    poll = spoll()
    poll.register(tsk.stdout,POLLIN | POLLHUP)
    poll.register(tsk.stderr,POLLIN | POLLHUP)
    pollc = 2
    events = poll.poll()
    tmp_out = NamedTemporaryFile(dir="/tmp", delete=False)
    tmp_err = NamedTemporaryFile(dir="/tmp", delete=False)
    chunk = []
    chunk_err = []
    while pollc > 0 and len(events) > 0:
        if len(chunk)>chunksize: 
            if cache:
                tmp_out.write("\n".join(chunk))
                tmp_err.write("\n".join(chunk_err))
                tmp_err.flush()
                tmp_out.flush()
            else:
                args[0]+=chunk
                args[1]+=chunk_err
            chunk = []
            chunk_err = []
        for rfd, event in events:
            if event & POLLIN:
                if rfd == tsk.stdout.fileno():
                    line = tsk.stdout.readline()
                    if len(line) > 0:
                        val = str(line[:-1])
                        if useLogging: logger.info(val)
                        chunk.append(val if suppressLevel else "INFO: %s"%val)
                if rfd == tsk.stderr.fileno():
                    line = tsk.stderr.readline()
                    if len(line) > 0:
                        if suppressErrors: continue
                        chunk_err.append(line[:-1])
                        val = chunk_err[-1] if len(chunk_err) else args[1][-1]
                        if useLogging: logger.error(val)
                        if interleaved: 
                            chunk.append(val if suppressLevel else "*ERROR*: %s"%val)
            if event & POLLHUP:
                poll.unregister(rfd)
                pollc = pollc - 1
            if pollc > 0: events = poll.poll()
    if cache:
        return tmp_out, tmp_err, tsk.wait()
    else:
        return "\n".join(args[0]), "\n".join(args[1]), tsk.wait()

def make_executable(path):
    mode = stat(path).st_mode
    mode |= (mode & 0o444) >> 2    # copy R bits to X
    chmod(path, mode)

def source_bash(setup_script):
    foop = open("tmp.sh", "w")
    foop.write("#/bin/bash\nsource $1\nenv|sort")
    foop.close()
    out, err, rc = run(["bash tmp.sh %s" % expandvars(setup_script)], useLogging=False, suppressErrors=True)
    if rc:
        print 'source encountered error, returning that one'
        return err
    lines = [l for l in out.split("\n") if "=" in l]
    keys, values = [], []
    for l in lines:
        tl = l.split("=")
        if len(tl) == 2:
            if tl[0].startswith("_"):
                continue
            if tl[0].startswith("BASH_FUNC"):
                continue
            keys.append(tl[0])
            values.append(tl[1])
    environ.update(dict(zip(keys, values)))
    remove("tmp.sh")
    return
