import csv
import sys
import os
import datetime


fileName = datetime.datetime.now().strftime('day'+'%Y_%m_%d')


def make_print_to_file(path='log/txt/'):
    '''
    path,  it is a path for save your log about fuction print
    example:
    use  make_print_to_file() and the all the information of funtion print , will be write in to a log file
    :return:
    '''
    if not os.path.exists(path):
        os.makedirs(path)
 
    class Logger(object):
        def __init__(self, filename="Default.log", path="./"):
            self.terminal = sys.stdout
            self.log = open(os.path.join(path, filename), "a", encoding='utf8',)
 
        def write(self, message):
            self.terminal.write(message)
            self.log.write(message)
 
        def flush(self):
            pass
 
    try:
        sys.stdout = Logger(fileName + '.log', path=path)
    except:
        sys.stdout = Logger(fileName + '/.log', path=path)

    
 
    #############################################################
    # 这里输出之后的所有的输出的print 内容即将写入日志
    #############################################################
    print(fileName.center(60,'*'))


def save_csv(data: dict, path='log/trade/'):
    if not os.path.exists(path):
        os.makedirs(path)

    csv_name = path+fileName+'.csv'
    if not os.path.exists(csv_name):
        exist = False
    else:
        exist = True

    exist_file = []
    if exist:
        with open(csv_name, "r") as f:
            reader = csv.reader(f, lineterminator="\n")
            for row in reader:
                exist_file.append(row)
            f.close()

    with open(csv_name, "w") as f:
        writer = csv.writer(f, lineterminator="\n")

        exist_file.append([v for v in data.values()])

        if not exist:
            writer.writerow([k for k in data.keys()])
        writer.writerows(exist_file)
        print(os.path.abspath(csv_name))
        print(exist_file)
        print('\n')
        f.close()