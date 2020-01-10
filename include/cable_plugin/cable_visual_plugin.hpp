#include <stdio.h>

#include <boost/bind.hpp>

#include <gazebo/common/common.hh>
#include <gazebo/common/Plugin.hh>
#include <gazebo/gazebo.hh>
#include "gazebo/msgs/msgs.hh"
#include <gazebo/physics/physics.hh>
#include "gazebo/transport/transport.hh"
#include <gazebo/rendering/rendering.hh>

#include <gazebo_msgs/LinkStates.h>

#include <ros/ros.h>
#include "ros/callback_queue.h"
#include "ros/subscribe_options.h"

#include <Eigen/Eigen>

#include "common.h"

#define M_PI 3.14159265358979323846

namespace gazebo {

class CableVisualPlugin : public VisualPlugin {
  public:
    CableVisualPlugin();
    virtual ~CableVisualPlugin();

  protected:
    virtual void Load(rendering::VisualPtr _visual, sdf::ElementPtr _sdf);
    virtual void Init();
    virtual void UpdateChild();
    virtual void UpdateVisual(const gazebo_msgs::LinkStatesConstPtr & _msg);
    virtual void QueueThread();

  private:
    std::string link_name1_;
    std::string link_name2_;
    std::string namespace_;

    std::unique_ptr<ros::NodeHandle> node_handle_;
    ros::Subscriber sub_;
    ros::CallbackQueue queue_;
    std::thread queue_thread_;

    // transport::NodePtr node_handle_;
    // transport::SubscriberPtr sub_;

    physics::ModelPtr model_;
    physics::LinkPtr link_;
    physics::LinkPtr link1_;
    physics::LinkPtr link2_;
    event::ConnectionPtr updateConnection_;

    rendering::VisualPtr visual_;
    rendering::DynamicLines *line_;
};
}
