# 研究现状

在少样本分类的元学习范式方面，研究者主要围绕模型参数的可适应性展开探索[1]。MAML通过优化初始参数，使模型仅需少量梯度更新即可在新任务上实现快速泛化，在Omniglot和mini-ImageNet等基准上验证了其有效性[2]；Meta-SGD进一步将学习率与更新方向纳入端到端优化，显著提升了少样本回归、分类及强化学习任务的性能[1]。这些工作共同表明，对模型初始化与优化过程的联合建模是提升元学习适应效率的关键路径[2][1]。

针对标注数据稀缺下的半监督与无监督扩展，研究逐步放松对每轮任务中标签数量的依赖[3][4]。Prototypical Networks的半监督变体通过端到端利用任务内无标签样本优化原型表示，在Omniglot与miniImageNet上实现了稳定增益[3]；UMTRA则完全摒弃任务级标注，借助无监督合成任务进行元训练，在Omniglot与Mini-ImageNet上显著降低对标注数据的依赖（减少3–4个数量级），尽管仍落后于监督方法[4]。总体来看，引入未标注信息已成为提升元学习在低资源场景下鲁棒性的主流策略[3][4]。

面向医学图像等真实稀缺领域的迁移应用也初具规模[5][6]。近期工作将ProtoNet与MAML应用于超声乳腺癌图像分类，通过跨域元训练缓解BUSI数据集类别数过少导致的新类划分困难，在BUSI上取得0.882–0.889的准确率与0.892的F1-score[5]。与此同时，图像增强技术作为数据匮乏下的通用补救手段，被系统梳理为几何变换、GAN生成与风格迁移等路径，并在CIFAR与SVHN等数据集上证实其可提升模型泛化能力[6]。然而，该综述亦指出，在数据极度稀缺时，盲目增强可能加剧过拟合[6]。

现有工作普遍存在三类共性局限：其一，多数元学习方法在未见任务分布或新领域上的泛化能力受限，尤其当元训练与元测试域差异较大时性能显著下降[1]；其二，当前半监督与无监督元学习方案虽降低标注依赖，但性能仍系统性低于全监督对应方法[4]；其三，真实医疗场景中因数据集固有结构（如BUSI仅含三类）导致无法构建标准元学习划分，迫使依赖跨域迁移，削弱了方法在目标域内的理论保障[5]。

## 参考文献

[1] Zhenguo Li, Fengwei Zhou, Fei Chen, et al. Meta-SGD: Learning to Learn Quickly for Few-Shot Learning[EB/OL]. arXiv (Cornell University), 2017. https://arxiv.org/pdf/1707.09835. DOI: 10.48550/arxiv.1707.09835.
[2] Chelsea Finn, Pieter Abbeel, Sergey Levine. Model-Agnostic Meta-Learning for Fast Adaptation of Deep Networks[EB/OL]. arXiv (Cornell University), 2017. https://arxiv.org/pdf/1703.03400. DOI: 10.48550/arxiv.1703.03400.
[3] Mengye Ren, Eleni Triantafillou, Sachin Ravi, et al. Meta-Learning for Semi-Supervised Few-Shot Classification[EB/OL]. arXiv (Cornell University), 2018. https://arxiv.org/pdf/1803.00676. DOI: 10.48550/arxiv.1803.00676.
[4] Siavash Khodadadeh, Ladislau Bölöni, Mubarak Shah. Unsupervised Meta-Learning For Few-Shot Image Classification[EB/OL]. arXiv (Cornell University), 2018. https://arxiv.org/pdf/1811.11819. DOI: 10.48550/arxiv.1811.11819.
[5] Gültekin Işık, İshak Paçal. Few-shot classification of ultrasound breast cancer images using meta-learning algorithms[J]. Neural Computing and Applications, 2024. DOI: 10.1007/s00521-024-09767-y.
[6] Connor Shorten, Taghi M. Khoshgoftaar. A survey on Image Data Augmentation for Deep Learning[J]. Journal Of Big Data, 2019. DOI: 10.1186/s40537-019-0197-0.