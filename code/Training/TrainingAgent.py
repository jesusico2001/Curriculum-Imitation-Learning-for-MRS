import os
import yaml
import torch

from Training.PathManager import PathManager
from Training.Teacher.TeacherBuilder import TeacherBuilder
from LearnSystem import LearnSystemBuilder
from Training.DatasetBuilder import DatasetBuilder

VAL_PERIOD = 50
class TrainingAgent():

    def __init__(self, path_config, config_changes=None):
        if isinstance(path_config, str):
            with open(path_config, "r") as file:
                config = yaml.safe_load(file)
            print("Config loaded...")

        if config_changes:
            for key, value in config_changes.items():
                keys = key.split('.')
                cfg = config
                for k in keys[:-1]:
                    cfg = cfg[k]
                cfg[keys[-1]] = value
                print("Config parameter updated: ", key, "=", value)
        
        self.seed_train = config["general"]["seed_train"]
        torch.manual_seed(self.seed_train)
        
        # Path manager
        self.path_manager = PathManager(config)

        # Student agent
        policy = config["learn_system"]["policy"]
        numAgents = config["learn_system"]["num_agents"]
        nAttLayers = config["learn_system"]["depth"]
        architecture = config["learn_system"]["type"]
        ls_parameters = LearnSystemBuilder.buildParameters(policy, numAgents, nAttLayers)
        self.learn_system = LearnSystemBuilder.buildLearnSystem(architecture, ls_parameters) 
        
        print("Student created...")

        # Student optimizer
        learning_rate   = self.learn_system.learning_rate
        self.optimizer = torch.optim.Adam(self.learn_system.parameters(), lr=learning_rate, weight_decay=0.0)

        # Teacher agent
        self.teacher = TeacherBuilder(config["teacher"])
        print("Teacher created...")

        # Dataset manager
        numTrain = config["general"]["train_size"]
        numVal = config["general"]["val_size"]
        seed_data = config["general"]["seed_data"]
        self.dataset_builder = DatasetBuilder(policy, numAgents, numTrain, numVal, seed_data, ls_parameters["device"])    


        self.device = ls_parameters["device"]
        self.epochs = config["general"]["epochs"]
        self.perform_early_stopping = config["general"]["early_stopping"]
        self.history = {
            "loss_train" : [],
            "loss_val_distr" : [],
            "difficulty_distr" : [],
            "val_epochs" : []
        }
        self.step_size       = 0.04
        self.numSamples_dataset = 250

        # Batch sizes
        self.train_size      = 100
        self.validation_size      = 100 * self.teacher.nDifficulties


    def checkExistingTraining(self):
        try:
            os.makedirs(self.path_manager.getPathCheckpoints())
            os.makedirs(self.path_manager.getPathHistory())
        except FileExistsError:
            
            act = input("There is already data for this configuration. Do you want to overwrite it? (Y/N)\n").lower()
            if act == "y":
                pass
            else:
                print("Aborting training...\n")
                exit(0)

    def trainingLoop(self):
        self.checkExistingTraining()

        # Datasets
        train_data, val_data = self.dataset_builder.BuildDatasets(self.numSamples_dataset)

        nextEpochVal = 0
        for epoch in range(0, self.epochs):

            # Run and compute loss
            difficulties = self.teacher.getDifficulties(self.train_size)
            inputs_train, target_train, top_difficulty = self.buildInputsTargets(train_data, self.train_size, difficulties)
            loss_train = self.runEpochLoss(inputs_train, target_train, difficulties, top_difficulty)

            # Update weights
            self.optimizer.zero_grad()
            loss_train.backward()
            self.optimizer.step()

            # Validate
            isValEpoch = False
            if epoch == nextEpochVal  or epoch == self.epochs-1 :
                isValEpoch = True  
                nextEpochVal += VAL_PERIOD
                
                print('Epoch = %d' % (epoch)  ) 
                print('- - - - - - - - -')
                print("|Training|\n  --Loss = ", loss_train.detach().cpu().numpy(), "\n  --Avg. Difficulty = ", difficulties.detach().cpu().numpy().mean())

                student_loss_distr = self.validate(val_data, self.validation_size)
                print('===============================================\n')
            
                # Store checkpoint 
                self.history["val_epochs"].append(epoch)
                self.history["loss_train"].append(loss_train)
                self.history["difficulty_distr"].append(self.teacher.getDifficultyDistribution())
                
                torch.save(self.learn_system.state_dict(), self.path_manager.getPathCheckpoints()+"/epoch_"+str(epoch)+'.pth')
                
                # Early stopping
                if self.perform_early_stopping:
                    print("Early stopping unavailable at the moment...")

            self.teacher.updateDifficulties(epoch, student_loss_distr, isValEpoch)
        print("Training Finished!")   

        self.saveHistory()     
        return

    def validate(self, val_data, validation_size):
        # Build targets and validate with maxNumSamples (normalized Loss)          
        difficulties = self.teacher.getValidationDifficulties(validation_size)
        inputs_val, target_val, top_difficulty = self.buildInputsTargets(val_data, validation_size, difficulties)
        val_loss_distr, avg_loss = self.valEpoch_loss_distr(inputs_val, target_val, difficulties, top_difficulty)

        self.history["loss_val_distr"].append(self.teacher.transformValLoss(val_loss_distr))
        print("|Validation|\n  --Loss = ", avg_loss, "\n  --Avg. Difficulty = ", difficulties.detach().cpu().numpy().mean())
        # print("  --Loss distr. = ", val_loss_distr)
        return val_loss_distr
    
    def buildInputsTargets(self, trajectories, batch_size, difficulties):
        # Select batch
        chosen_batch  = torch.randperm(trajectories.size(1))[:batch_size]
        batch = trajectories[:,chosen_batch,:]

        # Select initial states for traj. of numSamples
        top_difficulty = max(difficulties)
        realNS = trajectories.size()[0]
        chosen_initial_state  = torch.tensor([torch.randint(0, max(1, int(realNS-k)), [1]) for k in difficulties])
        chosen_states = chosen_initial_state.unsqueeze(1) + torch.arange(top_difficulty)
        chosen_states = [row[:size] for row, size in zip(chosen_states, difficulties)]

        # Build uniform unbiased batch
        numAgents = int(trajectories.size(2) / 8)
        inputs = torch.zeros([batch_size, 8*numAgents]).to(self.device)
        targets = torch.zeros([top_difficulty, batch_size, 4*numAgents]).to(self.device)
        
        for i in range(batch_size):
            inputs[i, :] = batch[chosen_initial_state[i], i, :]
            targets[:difficulties[i], i, :] = batch[chosen_states[i], i, :4*numAgents]
        
        return inputs, targets, top_difficulty

    def runEpochLoss(self, inputs, target, difficulties, max_difficulty):
        # Run epoch
        time            = self.step_size * max_difficulty
        simulation_time = torch.linspace(0, time - self.step_size, max_difficulty)

        output = self.learn_system.forward(inputs, simulation_time, self.step_size)
        
        # Mask lower difficulties with 0's at the end
        for i, ns in enumerate(difficulties):
            output[ns:,i,:] = torch.zeros([max_difficulty-ns, output.shape[2]])    

        # Compute loss
        L = self.L2_loss(output[:, :, :4 * self.learn_system.na], target, difficulties)
        return L


    def valEpoch_loss_distr(self, inputs_val, target_val, difficulties, max_difficulty):
        self.learn_system.eval()                 # Set evaluation mode
        
        # Compute trajectories
        time            = self.step_size * max_difficulty
        simulation_time = torch.linspace(0, time - self.step_size, max_difficulty)
        with torch.no_grad():
            output = self.learn_system.forward(inputs_val, simulation_time, self.step_size)

        # Mask lower difficulties with 0's at the end
        # TODO: Fix masking, now we mask everything with the easiest difficulty
        for i, ns in enumerate(difficulties):
            output[ns:,i,:] = torch.zeros([max_difficulty-ns, output.shape[2]])    

        # Raw losses
        losses = (output[:, :, :4 * self.learn_system.na] - target_val).pow(2)
        losses = losses.sum(dim=(0,2)) / target_val.shape[2]

        # Sum of losses for each difficulty
        loss_accumulated = torch.zeros(max_difficulty).to(self.device)
        for loss, difficulty in zip(losses, difficulties):
            loss_accumulated[difficulty-1] += loss
        
        difficulty_count = torch.bincount(difficulties-1).clone()

        # Mean loss for each difficulty
        loss_distr = torch.zeros(max_difficulty).to(self.device)
        for index_difficulty, loss in enumerate(loss_accumulated):
            difficulty = index_difficulty + 1
            loss_distr[index_difficulty] = loss / max(1,difficulty_count[index_difficulty] * difficulty)
        
        # Average losses (not consdering unsampled difficulties)
        avg_loss = loss_distr.sum() / torch.count_nonzero(difficulty_count)

        del difficulty_count, loss_accumulated, losses, output

        self.learn_system.train()                # Go back to training mode
    
        return loss_distr.detach().cpu().numpy(), avg_loss.detach().cpu().numpy()
    
    def L2_loss(self, u, v, ns_distr):
        sumErrors = torch.sum((u - v).pow(2)) 
        numComparedValues = (torch.sum(ns_distr) * u.shape[2])
        return sumErrors / numComparedValues
    
    def saveHistory(self):
        path = self.path_manager.getPathHistory()
        
        for key, value in self.history.items():
            torch.save(value, path+'/'+key+'.pth')

        print("History Saved!")
