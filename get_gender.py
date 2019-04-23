import csv

girls = set(["she", "She", "SHE", "her","Her","HER","woman", "Woman", "women", "Women", "WOMAN", "girl", "girls", "lady", "ladys"])
boys = set(["he", "He", "HE", "his", "His", "HIS", "man", "Man", "men", "Men", "MAN", "boy", "boys"])
train_file = open("/Users/weiyuwang/Desktop/data/val.csv")
train_file_reader = csv.reader(train_file, delimiter=',')
train_file_labeled = open("/Users/weiyuwang/Desktop/data/val_gendered.csv", "w")
train_file_writer = csv.writer(train_file_labeled, delimiter=',')

row = next(train_file_reader)
row_ = row + ["gender"]
train_file_writer.writerow(row_)
for row in train_file_reader:
    label = ""
    np = row[5]
    np = np.split(" ")
    for n in np:
        if n in girls:
            label = 1
        elif n in boys:
            label = 0
    if label != "":
        row_ = row + [label]
        train_file_writer.writerow(row_)


