# Research project

## Abstract

Due to operational or resource constraints, adopters of Ku-
bernetes (k8) do not always have the ability to operate their
clusters on bare metal hardware and often rely on virtualized
solutions to enforce separation and high availability. Previous
work demonstrated that these networking abstractions can in-
troduce measurable overhead in both the networking stack by
introducing redundant packet-processing. This paper focuses
on the performance and operational differences between using
CNI-created virtual networks, or by leveraging the underlying
virtual private cloud (VPC) resources to tunnel packets be-
tween nodes. We will evaluate these alternatives using Kuber-
netes worker VMs deployed across CloudLab physical hosts,
measuring latency, throughput, CPU utilization, forwarding
state, and routing convergence while preserving equivalent
Kubernetes networking semantics.

## Plan

##### Phase 1
Oct. 18: CloudLab and VM provisioning 
- Reproducible artifacts for provisioning two CloudLab hosts, deploying KVM virtual machines, and verifying inter-VM connectivity.
- Nov. 1 Kubernetes and Cilium VXLAN Reproducible Kubernetes cluster deployment across worker VMs, with Cilium VXLAN providing inter-node Pod connectivity.
Nov. 8 Measurement infrastructure
- Automated latency, throughput, CPU utilization, and packet capture utilities,
with preliminary VXLAN baseline measurements.
Nov. 10 Midterm report
- Documented architecture, literature review, implementation progress, and
preliminary experimental results.

##### Phase 2
Nov. 15 VPC-native routing prototype 
- Implementation of Cilium native routing with hypervisor-managed Pod-
prefix forwarding and verification of inter-node connectivity.
Nov. 19 Exam 
- Reduced project activity to accommodate exam preparation.
Nov. 27 Routing validation and experiments
- Validation of equivalent forwarding semantics and initial comparative performance measurements for both architectures.
Nov. 30 Presentation and contingency buffer
- Prepared project presentation, preliminary analysis, and additional time for troubleshooting.
Dec. 4 Final experimental measurements
- Completed repeated benchmarks, collected performance data, and finalized experimental configurations.
Dec. 7 Paper draft 
- Complete draft containing methodology, experimental results, analysis, discussion, and limitations.
Dec. 9 Review and revision 
- Incorporation of feedback and final verification of figures, citations, and reproducibility artifacts.
Dec. 11 Final submission 
- Submission of the completed research paper and supporting experimental artifacts

