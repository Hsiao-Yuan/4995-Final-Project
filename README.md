# 4995-Final-Project
pytorch_pretrained_bert: an open-source implementation of pretrained BERT in PyTorch. https://github.com/rodgzilla/pytorch-pretrained-BERT.
run_adversary_all.py: train or evaluate an adversary on all possible sentences. Used in the first experiment.
run_predictor.py: train or evaluate a predictor without any adversary. Used in the second experiment as control group.
run_adversary_gold.py: train or evaluate an adversary on the correct sentences. Used in the second experiment as control group.
run_debiasing.py: perform an adversarial debiasing. Used in the second experiment as experimental group.
new_debiasing.py: perform a modified adversarial debiasing. Used in the third experiment.
train.csv: training set with gender label and neutral gender excluded.
val.csv: validation set with gender label and neutral gender excluded.
