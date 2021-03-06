import numpy as np
import pandas as pd
import os
import traceback
import utils
import rpy2
import rpy2.robjects as robjects
import rpy2.robjects.packages as rpackages
import rpy2.rinterface as rinterface
import ggplot as gg
import time

from rpy2.robjects import pandas2ri, numpy2ri
from rpy2.robjects.packages import importr
from rpy2.robjects.vectors import StrVector, IntVector


# TODO: Add _generate_rules() to the rules getter, if it does not already exist
class RuleFit(object):
    """Wrapper for rulefit algorithm in R
    """

    def __init__(self, platform, rfhome, log_path=None):
        # Initialize R instance.
        self._initialize_r_instance(platform, rfhome)
        self.logger = utils.get_logger(log_path if log_path else \
                                           os.path.join(os.getcwd(),
                                                        'rulefit.log'))

    @property
    def rules(self):
        return self._rules

    @property
    def xval_results(self):
        return self._xval_results

    @property
    def data(self):
        return self._data

    @property
    def variable_importances(self):
        return self._variable_importances

    @property
    def interaction_effects(self):
        return self._interaction_effects

    def _initialize_r_instance(self, platform, rfhome):
        """Initialize R instance, declare global vars, and import rulefit
        Args:
          platform - OS. windows, linux or mac
          rfhome - path to R rulefit directory.
        """
        pandas2ri.activate()
        numpy2ri.activate()
        try:
            utils = importr('utils')
            utils.chooseCRANmirror(ind=1)
            robjects.globalenv['platform'] = platform
            robjects.globalenv['rfhome'] = rfhome
            self.rfhome = rfhome
            self.platform = platform
            import_str = """
                   source(paste(rfhome, '/rulefit.r', sep=''))
                   install.packages('akima', lib=rfhome)
                   library(akima, lib.loc=rfhome)
                   """
            robjects.r(import_str)
        except rpy2.rinterface.RRuntimeError as e:
            self.logger.error('RRuntimeError thrown. Try to re-run or check R'
                              ' rulefit path. Traceback: {}' \
                              .format(traceback.format_exc()))

    def _load_r_variable_importance_objects(self):
        """ Loads R variable importance objects

        Returns: Pandas dataframe of variable importances
        """
        data = [value for value in list(robjects.globalenv['var_imp'][0])]
        index = [self.data['x'].columns.values[int(var - 1)]
                 for var in list(robjects.globalenv['var_imp'][1])]

        temp_var_imp = pd.DataFrame(data=data, index=index,
                                    columns=['variable_importance'])
        temp_var_imp = temp_var_imp.reset_index() \
            .rename(columns={'index': 'var_name'})
        return temp_var_imp

    def _update_model_properties(self, x, y):
        """ Updates internal values after fitting
        """

        try:
            self._data['x'] = x
            self._data['y'] = y
        except:
            self._data = {}
            self._data['x'] = x
            self._data['y'] = y

        self._variable_importances = self._load_r_variable_importance_objects()

    # ===== Variable Interactions =====
    def _generate_interaction_null_models(self, n, quiet):
        """ Generates bootstrapped null interaction models to calibrate
            interaction effects. See FP 2004 8.3. This will create a global object
            in the R instance. This function needs to be called before calling
            interaction functions.
        Args:
          n - Number of models to generate
          quiet - True / False
        """
        null_str = """
               function(n, quiet){
                 null.models <<- intnull(ntimes=n, quiet=quiet)
               }
               """
        robjects.r(null_str)(n, quiet)

    def generate_intr_effects(self, nval=10, n=10, quiet=False, plot=True):
        """ Loads R variable interaction effect objects
        Args:
          nval - Number of evaluation points used for calculation
          n - Number of null models to generate for interaction calibaration
          quiet - Determines whether to print intermediate data.
        Returns: Pandas dataframe of interaction effects
        """

        self._generate_interaction_null_models(n, quiet)

        int_str = """
              function(ncols, nval){
                if(exists("null.models")){
                  interactions <- interact(c(1:ncols), null.models, 
                                           nval=nval, 
                                           plot=F)
                  } else {
                    interactions <- interact(c(1:ncols), nval=nval, plot=F)
                }
              }
              """
        ncols = len(self._data['x'].columns.values)
        r_interact = robjects.r(int_str)(ncols, nval)

        interact = pd.DataFrame({
            'interact_str': list(r_interact[0]),
            'exp_null_int': list(r_interact[1]),
            'std_null_int': list(r_interact[2])
        },
                index=self._data['x'].columns)

        self._interaction_effects = interact

        if plot:
            int_effects = interact.reset_index().rename(
                    columns={'index': 'vars'})
            int_effects_m = pd.melt(int_effects, id_vars='vars',
                                    value_vars=['interact_str', 'exp_null_int'])
            p = gg.ggplot(gg.aes(x='vars', fill='variable', weight='value'),
                          data=int_effects_m) \
                + gg.geom_bar() \
                + gg(title='Interaction Effects')
            print(p)

    def two_var_intr_effects(self, target, vars, nval=100, plot=True):
        """ Loads first level interactions.
        Args:
          target - Variable identifier (column name or number) specifying the
                   target variable
          vars - List of variable identifiers (column names or numbers) specifying
                 other selected variables. Must not contain target
          nval - Number of evaluation points used for calculation.
          plot - Determines whether or not to plot results.
        Returns: Pandas dataframe of interaction effects
        """
        # Check if null.models have already been generated
        check_str = """
                function(){
                  if(exists("null.models")){
                    return(T)
                  } else {
                    return(F)
                  }
                }
                """
        if not robjects.r(check_str)()[0]:
            self.logger.info(
                    'Null models not generated, generating null models '
                    '(n=10)')
            self._generate_interaction_null_models(10, quiet=False)

        int_str = """
              function(target, vars, nval){
                interactions <- twovarint(tvar=target, vars=vars, null.models, 
                                          nval=nval, plot=F)
              }
              """
        # Check the input type. If int, add one, if string do nothing.
        target = target if type(target) is str else target + 1
        vars = [var if type(var) is str else var + 1 for var in vars]
        r_interact = robjects.r(int_str)(target,
                                         robjects.Vector(np.array(vars)),
                                         nval)
        interact = pd.DataFrame({
            'interact_str': list(r_interact[0]),
            'exp_null_int': list(r_interact[1]),
            'std_null_int': list(r_interact[2])
        },
                index=vars)

        if plot:
            int_effects = interact.reset_index().rename(
                    columns={'index': 'vars'})
            int_effects_m = pd.melt(int_effects, id_vars='vars',
                                    value_vars=['interact_str', 'exp_null_int'])
            p = gg.ggplot(gg.aes(x='vars', fill='variable', weight='value'),
                          data=int_effects_m) \
                + gg.geom_bar() \
                + gg.labs(
                    title='Two-var interaction effects - {}'.format(target))
            print(p)
        return interact

    def three_var_intr_effects(self, tvar1, tvar2, vars, nval=100, plot=True):
        """ Loads second level interactions between 3 variables
        Args:
          tvar1 - Variable identifier (column name or number) specifying the
                 first target variable
          tvar2 - Variable identifier (column name or number) specifying the
                 second target variable
          vars - List of variable identifiers (column names or numbers) specifying
                 other selected variables. Must not contain target.
          nval - Number of evaluation points used for calculation.
          plot - Determines whether or not to plot results.
        Returns:
          Pandas dataframe of interaction effects
        """
        # Check if null.models have already been generated
        check_str = """
                function(){
                  if(exists("null.models")){
                    return(T)
                  } else {
                    return(F)
                  }
                }
                """
        if not robjects.r(check_str)()[0]:
            self.logger.info(
                    'Null models not generated, generating null models '
                    '(n=10)')
            self._generate_interaction_null_models(10, quiet=False)

        int_str = """
              function(tvar1, tvar2, vars, nval){
                interactions <- threevarint(tvar1=tvar1, tvar2=tvar2,
                                            vars=vars, null.models, 
                                            nval=nval, plot=F)
              }
              """
        # Check the input type. If int, add one, if string do nothing.
        tvar1 = tvar1 if type(tvar1) is str else tvar1 + 1
        tvar2 = tvar2 if type(tvar2) is str else tvar2 + 1
        vars = [var if type(var) is str else var + 1 for var in vars]
        r_interact = robjects.r(int_str)(tvar1, tvar2,
                                         robjects.Vector(np.array(vars)), nval)
        interact = pd.DataFrame({
            'interact_str': list(r_interact[0]),
            'exp_null_int': list(r_interact[1]),
            'std_null_int': list(r_interact[2])
        },
                index=vars)

        if plot:
            int_effects = interact.reset_index().rename(
                    columns={'index': 'vars'})
            int_effects_m = pd.melt(int_effects, id_vars='vars',
                                    value_vars=['interact_str', 'exp_null_int'])
            p = gg.ggplot(gg.aes(x='vars', fill='variable', weight='value'),
                          data=int_effects_m) \
                + gg.geom_bar() \
                + gg.labs(title='Three-var interaction effects - {} {}' \
                          .format(tvar1, tvar2))
            print(p)
        return interact

    # ===== Partial Dependancies =====
    def single_partial_dependency(self, vars, nav=500):
        """ Display single variable partial dependancy plots
        Args:
          vars - A list of variable indecies or column names indicated which plots
                 to print.
          nav - Maximum number of observations used for average calculations.
                Higher values give more accurate calculations with diminishing
                returns.
        """
        dep_str = """
              function(vars, nval, nav){
                singleplot(vars)
              }
              """
        if any([type(var) == int for var in vars]):
            vars = [var + 1 for var in vars]
        robjects.r(dep_str)(robjects.Vector(np.array(vars)), nav)

    def double_partial_dependencies(self, var1, var2, plot_type='image'):
        """ Display two variable dependancy plot.
        Args:
          var1 - First variable for calculation
          var2 - Second variable for calculation. Cannot be the same as the first.
          plot_type - The top of plot to print. Possible values: 'image', 'contour'
        """
        dep_str = """
              function(var1, var2, plot_type){
                pairplot(var1, var2, type=plot_type)
              }
              """
        robjects.r(dep_str)(var1, var2, plot_type)

    def _generate_rules(self, x=None, wt=None):
        """ Extract generated rules from model object. This populations the rules
            property.
        Args:
          beg - Starting rule number (most important)
          end - Ending
          x - A pandas dataframe containing a subset of datapoints over which to
              calculate rule importance.
          wt - A list of weights for observations stored in x.
        """

        if not wt:
            wt = np.arange(1, self.data['x'].shape[0] + 1)
        if not x:
            x = rinterface.NULL

        rules_str = """
                function(x, wt){
                  beg = 1
                  end = 99999999
                  if(end > integer(fit[[1]][length(fit[[1]])])){
                    end <- as.integer(fit[[1]][length(fit[[1]])])
                  } 
                  if(!is.null(x)){
                    rules(beg, end, x, wt)
                  } else {
                    rules(beg=beg, end=end, wt=wt)
                  }
                }
                """
        self.logger.info("Generating rules ...")
        robjects.r(rules_str)(x, wt)
        self._rules = utils.parse_rules(
                os.path.join(self.rfhome, 'rulesout.hlp'))

    def predict(self, x):
        """ Predict values using a trained model
        Args:
          x - A pandas dataframe of input variables
        Returns:
          A list of response values. If classifications will return the
          log odds. The corresponding probabilities can be computed with
          probs = 1.0 / (1.0 + exp(-pred))
        """
        predict_str = """
                  function(xp){
                  yp <<- rfpred(xp)
                  }
                  """
        predict = np.array(robjects.r(predict_str)(x))
        return predict

    def xval(self, nfold=10, quiet=False):
        """ Performs cross validation using current model. Will update
            corresponding properties in rulefit object
        Args:
          nfold - Number of folds >= 2
          quiet - True or False
        Returns:
          Nothing. But will update the properties x_val_results
        """

        xval_str = """
               function(nfold, quiet){
               xval <<- rfxval(nfold, quiet)
               }
               """
        xval = robjects.r(xval_str)(nfold, quiet)
        # Populate xval values
        if robjects.r['length'](xval)[0] == 5:  # Classification
            self._xval_results = {
                'probas': list(xval[0]),
                'auc': 1 - xval[1][0],
                'avg_err': xval[2][0],
                'pos_err': xval[3][0],
                'neg_err': xval[4][0]
            }
        else:  # Regression
            self._xval_results = {
                'pred': list(xval[0]),
                'avg_abs_err': xval[1][0],
                'rms': xval[2][0]
            }

    def fit(self, x, y, wt=None, cat_vars=None, not_used=None,
            xmiss=9.0e30, rfmode='class', sparse=1, test_reps=None,
            test_fract=0.2, mod_sel=3, model_type='both', tree_size=4,
            max_rules=2000, max_trms=500, costs=[1, 1], trim_qntl=0.025,
            samp_fract=None, inter_supp=3.0, memory_par=0.01, conv_thr=1.0e-3,
            quiet=False, tree_store=10e6, cat_store=10e6):
        """ Fit rulefit model. This function will populate the data and variable
            importance fields of the Rulefit object.
        Args:
          x -             Pandas dataframe of training data.
          y -             Input response values. For classification values
                          must be only 1 or -1. If y is a single scalar, it will
                          will be interpreted as a column number.
          wt -            Observation weights. If wt is a single valued
                          scalar it is interpreted as a label (number or
                          name) referencing a column of x. Otherwise it is a
                          vector of length nrow(x) containing the numeric
                          observation weights.
                          Default: wt = np.arange(1, x.shape[0])
          cat_vars -      List of column labels (numbers or names) indicating
                          categorical variables (factors). All variables not
                          so indicated are assumed to be orderable numeric.
                          If x is a data frame and cat_vars is missing, then
                          components of type categorical are treated as
                          categorical variables.
          not_used -      List of column labels (numbers or names) indicating
                          predictor variables not to be used in the model.
          xmiss -         Predictor variable missing value flag. Predictor
                          variable values greater than xmiss are regarded as
                          missing.
          rfmode -        ('regress', 'class') Default: 'class'
          sparse -        Model sparisty control. Larger values produce
                          sparser models, smaller values produce denser
                          models.
                          (0, 1) -> elastic net regression. alpha = sparse
                          1 -> lasso regression
                          2 -> lasso to select variable entry order for
                               forward stepwise regression.
                          3 -> forward stepwise (regression) or forward
                               stagewise (classification)
          test_reps -     Number of CV replications used for model selection.
                          0 - No CV procedure
                          >0 - test_reps fold cross validation. Final model
                               is based on the whole training sample. Default
                               value refers to number of effective training
                               observations neff = sum(wt)^2 / sum(wt^2)
                               for regression. For classification
                          4 * fpos * (1 - fpos), fpos=total+1 labels/total labels
                          Default: test_reps =
                                        round(min(20, max(0.0, 5200 / neff - 2)))
          test_fract -    Fraction of input observations used in test sample.
          mode_sel -      Model selection criteria.
                          1 -> regres: average absolute error loss
                               class: correlation criterion (similar
                                      to 1 - AUC)
                          2 -> regres: average squared error loss
                               class: average sq error loss on predicted probas
                          3 -> misclassification risk
          model_type -    Determines model type.
                          linear -> Only use original linear variables
                          rules -> Only used generated rules (non-linear)
                          both -> Use both.
          tree_size -     Average number of terminal nodes in generated trees
                          (see FP 2004, sec 3.3)
          max_rules -     Approximate number of rules generated for fitting
          max_trms -      Number of terms selected for final model
          costs -         misclassification costs (only for mod_sel = 3).
                          costs[0] = cost for class +1, costs[1] = cost for
                          class -1.
          trim_qntl -     Linear variable conditioning factor. Ignored
                          for model_type = 'rules'. (see FP 2004, sec 5)
          samp_fract -    Fraction of randomly chosen training obs used to
                          produce each tree (see FP 2004, sec 2). Default
                          value refers to neff, see "test_reps".
                          rules (see FP 2004, sec 8.2)
          memory_par -    Learning rate applied to each new tree when
                          sequentially induced. (see FP 2004 sec 2)
          conv_thr -      Convergence threshold for regression solutions.
          quiet -         True, False
          tree_store -    Size of internal tree storage. Decrease value in
                          response to memory allocation error. Increase value
                          for very large values of max_rules / tree_size.
          cat_store -     Size of internal categorical value store. Decrease
                          value in response to memory allocation error.
                          Increase valu for very large values of
                          max_rules / tree_size
        Returns:
          A tuple of (cross-validated criterion value, associated
          uncertainty estimate, number of terms in the model)
        """

        # Set default values
        # Need to convert None values to r null type
        if type(y) == int:  # Add one to y if its int as Rs index start at 1
            y += 1
        if wt is None:
            wt = np.arange(1, x.shape[0] + 1)
        if test_reps is None:
            neff = np.sum(wt) ** 2 / np.sum(wt ** 2)
            test_reps = round(min(20, np.max([0.0, 5200 / neff - 2])))
        if samp_fract is None:
            neff = np.sum(wt) ** 2 / np.sum(wt ** 2)
            samp_fract = min(1, (11 * np.sqrt(neff) + 1) / neff)
        if cat_vars is None:
            cat_vars = rinterface.NULL
        if not_used is None:
            not_used = rinterface.NULL

        # Cant pass null default values to r function so those variables
        # need to be not passed.
        rulefit_str = """
                  function(x, y, wt, cat_vars, not_used, xmiss, rfmode,
                           sparse, test_reps, test_fract, mod_sel,
                           model_type, tree_size, max_rules, max_trms,
                           costs, trim_qntl, samp_fract, inter_supp,
                           memory_par, conv_thr, quiet, tree_store, 
                           cat_store){

                    costs = c(costs[[1]], costs[[2]]) 
                    args = list(x=x, y=y, wt=wt, cat.vars=cat_vars,
                                not.used=not_used, xmiss=xmiss, rfmode=rfmode,
                                sparse=sparse, test.reps=test_reps, 
                                test.fract=test_fract, mod.sel=mod_sel,
                                model.type=model_type, tree.size=tree_size,
                                max.rules=max_rules, max.trms=max_trms,
                                costs=costs, trim.qntl=trim_qntl, 
                                samp.fract=samp_fract, inter.supp=inter_supp,
                                memory.par=memory_par, conv.thr=conv_thr,
                                quiet=quiet, tree.store=tree_store,
                                cat.store=cat_store)
                    count = 1
                    for (i in 1:length(args)){
                      if(is.null(args[[count]])){
                        args[[count]] <- NULL
                      } else {
                        count = count + 1 
                      }
                    }

                    fit <<- do.call(rulefit, args)
                    stats <<- runstats(fit) 
                    var_imp <<- varimp(plot=F)
                  }
                  """
        # Run rulefit model
        rulefit = robjects.r(rulefit_str)
        fit = rulefit(x, y, wt=wt, cat_vars=cat_vars, not_used=not_used,
                      xmiss=xmiss, rfmode=rfmode, sparse=sparse,
                      test_reps=test_reps, test_fract=test_fract,
                      mod_sel=mod_sel, model_type=model_type,
                      tree_size=tree_size, max_rules=max_rules,
                      max_trms=max_trms, costs=costs, trim_qntl=trim_qntl,
                      samp_fract=samp_fract, inter_supp=inter_supp,
                      memory_par=memory_par, conv_thr=conv_thr, quiet=quiet,
                      tree_store=tree_store, cat_store=cat_store)

        # Update model properties
        self._update_model_properties(x, y)

        # Output fit statistics
        fit_stats = (robjects.globalenv['stats'][0][0],
                     robjects.globalenv['stats'][1][0],
                     robjects.globalenv['stats'][2][0])

        return fit_stats
