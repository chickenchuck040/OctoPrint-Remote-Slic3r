
import ConfigParser

config = ConfigParser.ConfigParser()

# Slic3r does not add a section header, a class to add one if needed
# http://stackoverflow.com/questions/2819696/parsing-properties-file-in-python/2819788#2819788
class FakeSecHead(object):
    def __init__(self, fp):
        self.fp = fp
        self.sechead = '[all]\n'
    def readline(self):
        if self.sechead:
            try: return self.sechead
            finally: self.sechead = None
        else: return self.fp.readline()

def import_ini(config_path):
    global config
    # Add a dummy section header if needed
    try:
        config.read(config_path)
    except ConfigParser.MissingSectionHeaderError:
        config.readfp(FakeSecHead(open(config_path)))

def export_ini(export_path, slic3r):
    global config
    tmpfile = open("/tmp/tmp.ini", "w+")
    outfile = open(export_path, "w+")
    config.write(tmpfile)
    tmpfile.seek(0)
    for line in tmpfile:
        if slic3r:
            if not line.startswith("["):
                outfile.write(line)
        else:
            outfile.write(line)

    outfile.flush()
    outfile.close()

def get_info()

#import_ini("profiles/config.ini")
#export_ini("profiles/slic3rconfig.ini", True)
