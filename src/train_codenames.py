import torch
from torch.optim.lr_scheduler import ExponentialLR
from loss_fns.loss import  RewardSearchLoss, RewardSearchWithWassersteinLoss
from torch.utils.data import DataLoader
from models.multi_objective_models import MORSpyMaster
from datasets.dataset import CodeNamesDataset
import numpy as np
import datetime
import argparse
import utils.utilities as utils
from utils.vector_search import VectorSearch
from utils.hidden_vars import BASE_DIR
import utils.utilities as utils
from utils.logger import EpochLogger

def init_hyperparameters(model: MORSpyMaster, device, normalize_reward):
    loss_fn = RewardSearchLoss(model_marg=0.7, search_marg=0.8, device=device, normalize=normalize_reward)
    optimizer = torch.optim.AdamW(model.parameters(), lr=0.00001, weight_decay=0.1)
    scheduler = ExponentialLR(optimizer, gamma=0.9)
    return loss_fn, optimizer, scheduler

class LossResults:
    def __init__(self, data_size: int) -> None:
        self.tot_pos = 0.0
        self.tot_neg = 0.0
        self.tot_neut = 0.0
        self.size = data_size
    
    def add_results(self, results: tuple):
        assert len(results) == 3
        pos, neg, neut = results

        self.tot_pos += pos.mean(0).item()
        self.tot_neg += neg.mean(0).item()
        self.tot_neut += neut.mean(0).item()
    
    @property
    def results_str(self) -> str:
        return f"Positive: {self.tot_pos/self.size}, Negative: {self.tot_neg/self.size}, Neutral: {self.tot_neut/self.size}"

@torch.no_grad()
def validate(model: MORSpyMaster, valid_loader: DataLoader, loss_fn: RewardSearchLoss, device: torch.device):
    val_logger = EpochLogger(len(valid_loader.dataset), len(valid_loader), device, "Validation")

    for i, data in enumerate(valid_loader, 0):
        pos_embeddings, neg_embeddings, neut_embeddings, assas_embeddings = data[1]
        pos_embeddings, neg_embeddings, neut_embeddings, assas_embeddings = pos_embeddings.to(device), neg_embeddings.to(device), neut_embeddings.to(device), assas_embeddings.to(device)

        model_out, search_out, search_out_max, search_out_min = model(pos_embeddings, neg_embeddings, neut_embeddings, assas_embeddings)

        #loss = F.triplet_margin_loss(model_out, search_min, search_max, margin=0.2)
        loss = loss_fn(model_out, search_out_max, search_out_min, pos_embeddings, neg_embeddings, neut_embeddings, assas_embeddings)
        #score, results, neut_sum, assas_sum = utils.calc_codenames_score(search_out, pos_embeddings, neg_embeddings, neut_embeddings, assas_embeddings, device)

        val_logger.update_results(search_out, pos_embeddings, neg_embeddings, neut_embeddings, assas_embeddings)
        val_logger.update_loss(loss)

    return val_logger

def train(n_epochs: int, model: MORSpyMaster, train_loader: DataLoader, valid_dataloader: DataLoader, device: torch.device, model_path: str, normalize_reward: bool, use_model_out: bool):
    loss_fn, optimizer, scheduler = init_hyperparameters(model, device, normalize_reward)
    print("Training")
    model.train()

    losses_train = []
    losses_valid = []

    print(f"Starting training at: {datetime.datetime.now()}")
    for epoch in range(1, n_epochs + 1):
        print(f"Epoch: {epoch}")
        train_logger_search = EpochLogger(len(train_loader.dataset), len(train_loader), device, name="Training with Search Output")
        train_logger_model = EpochLogger(len(train_loader.dataset), len(train_loader), device, name="Training with Model Output")
        for i, data in enumerate(train_loader, 0):
            if (i % 100 == 0):
                print(f"{datetime.datetime.now()}: Iteration: {i}/{len(train_loader)}")
            pos_embeddings, neg_embeddings, neut_embeddings, assas_embeddings = data[1]
            # Put embeddings on device
            pos_embeddings = pos_embeddings.to(device)
            neg_embeddings = neg_embeddings.to(device)
            neut_embeddings = neut_embeddings.to(device)
            assas_embeddings = assas_embeddings.to(device)
            
            optimizer.zero_grad()
            model_out, search_out, search_out_max, search_out_min = model(pos_embeddings, neg_embeddings, neut_embeddings, assas_embeddings)

            loss = loss_fn(model_out, search_out_max, search_out_min, pos_embeddings, neg_embeddings, neut_embeddings, assas_embeddings)
            
            # score_input = search_out
            
            # if use_model_out:
            #     score_input = model_out

            # score, neg_sum, neut_sum, assas_sum = utils.calc_codenames_score(score_input, pos_embeddings, neg_embeddings, neut_embeddings, assas_embeddings, device)
            

            train_logger_model.update_results(model_out, pos_embeddings, neg_embeddings, neut_embeddings, assas_embeddings)
            train_logger_search.update_results(search_out, pos_embeddings, neg_embeddings, neut_embeddings, assas_embeddings)

            loss.backward()
            optimizer.step()
            
        scheduler.step()
        
        # Validate model output
        valid_logger = validate(model, valid_dataloader, loss_fn, device)
        losses_train.append(train_logger_search.avg_loss)
        losses_valid.append(valid_logger.avg_loss)

        print()
        train_logger_model.print_log()
        train_logger_search.print_log()
        valid_logger.print_log()
        
        if len(losses_train) == 1 or losses_train[-1] < losses_train[-2]:
            torch.save(model.state_dict(), model_path)
        
    return losses_train, losses_valid

def main(args):
    device = utils.get_device(args.cuda)
    # extract cli arguments
    code_data = args.code_data
    guess_data = args.guess_data
    val_guess_data = args.val_guess_data
    model_out = args.model_out
    loss_out = args.loss_out
    vocab_size = args.vocab

    use_model_output = utils.convert_args_str_to_bool(args.use_model_out)
    search_pruning = utils.convert_args_str_to_bool(args.prune_search)
    normalize_reward = utils.convert_args_str_to_bool(args.norm)

    print(f"Device: {device}")
    train_dataset = CodeNamesDataset(code_dir=code_data, game_dir=guess_data)
    train_dataloader = DataLoader(train_dataset, batch_size=args.b, num_workers=4)
    print(f"Training Length: {len(train_dataset)}")
    valid_dataset = CodeNamesDataset(code_dir=code_data, game_dir=val_guess_data)
    valid_dataloader = DataLoader(valid_dataset, batch_size=50, num_workers=4)

    vector_db = VectorSearch(train_dataset, prune=True)
    model = MORSpyMaster(vector_db, device, vocab_size=vocab_size, search_pruning=search_pruning)
    model.to(device)

    losses_train, losses_valid = train(n_epochs=args.e, model=model, train_loader=train_dataloader, valid_dataloader=valid_dataloader, device=device, model_path=model_out, normalize_reward=normalize_reward, use_model_out=use_model_output)
    utils.save_loss_plot(losses_train, losses_valid, save_path=loss_out)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('-e', type=int, help="Number of epochs", default=10)
    parser.add_argument('-b', type=int, help="Batch Size", default=400)
    parser.add_argument('-code_data', type=str, help="Codenames dataset path", default=BASE_DIR + "data/words.json")
    parser.add_argument('-guess_data', type=str, help="Geuss words dataset path", default=BASE_DIR + "data/codewords_full_w_assassin_valid.json")
    parser.add_argument('-vocab', type=int, default=80)
    parser.add_argument('-weight_decay', type=float, default=0.1)
    parser.add_argument('-prune_search', type=str, help="Prunes the search window based on average similarity [Y/n]", default='N')
    parser.add_argument('-use_model_out', type=str, help="Determines whether to use the model output for scoring or the search output (highest scoring word embedding) [Y/n]", default='N')
    parser.add_argument('-gamma', type=float, default=0.9)
    parser.add_argument('-norm', type=str, help="Whether to normalize reward function, [Y/n]", default='Y')
    parser.add_argument('-val_guess_data', type=str, help="Filepath for the validation dataset", default=BASE_DIR + "data/codewords_full_w_assassin_mini.json")
    parser.add_argument('-model_out', type=str, default=BASE_DIR + "test.pth")
    parser.add_argument('-loss_out', type=str, default=BASE_DIR + "test.png")
    parser.add_argument('-cuda', type=str, help="Whether to use CPU or Cuda, use Y or N", default='Y')
    args = parser.parse_args()
    main(args)